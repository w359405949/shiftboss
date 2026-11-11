"""只读 TypeScript 模块语法与运行/类型导入信息；不执行产品模块。"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from shiftboss import config

TYPESCRIPT_SUFFIXES = (".ts", ".mts", ".cts")
MODULE_SUFFIXES = (".js", ".mjs", ".cjs", *TYPESCRIPT_SUFFIXES)


def runtime_names(name: str) -> set[str]:
    """改过 TS 源时也查仍使用输出扩展名的调用方。"""
    path = Path(name)
    extension = {".ts": ".js", ".mts": ".mjs", ".cts": ".cjs"}.get(path.suffix)
    return {path.name, path.with_suffix(extension).name} if extension else {path.name}


def source_module(path: Path) -> Path:
    """与执行链一样只接受唯一真实源，拒绝同名 JS/TS 双实现。"""
    pairs = {".js": ".ts", ".mjs": ".mts", ".cjs": ".cts"}
    reverse = {typed: runtime for runtime, typed in pairs.items()}
    logical = path.with_suffix(reverse[path.suffix]) if path.suffix in reverse else path
    extension = pairs.get(logical.suffix)
    candidate = logical.with_suffix(extension) if extension else logical
    if candidate != logical and logical.is_file() and candidate.is_file():
        raise ValueError(f"同名 JS/TS 双实现：{logical}、{candidate}")
    return candidate if candidate.is_file() else path


_SCRIPT = r"""
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {resolve, relative} from 'node:path';
import {SourceTextModule} from 'node:vm';
const [compiler, root, ...requested] = process.argv.slice(2);
const ts = createRequire(import.meta.url)(compiler);
const options = {allowJs: true, moduleResolution: ts.ModuleResolutionKind.Bundler,
  module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ESNext};
const configName = ts.findConfigFile(root, ts.sys.fileExists);
if (configName) {
  const config = ts.readConfigFile(configName, ts.sys.readFile);
  if (config.error) throw new Error(ts.flattenDiagnosticMessageText(config.error.messageText, '\n'));
  const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, root);
  if (parsed.errors.length) throw new Error(parsed.errors.map(e => ts.flattenDiagnosticMessageText(e.messageText, '\n')).join('\n'));
  Object.assign(options, parsed.options);
}
const result = {};
const key = file => relative(root, file).split('\\').join('/');
const bindingNames = node => ts.isIdentifier(node) ? [node.text]
  : node.elements.flatMap(element => ts.isOmittedExpression(element) ? [] : bindingNames(element.name));
const modifier = (node, kind) => node.modifiers?.some(value => value.kind === kind);
function inspect(name, scanImports) {
  const file = resolve(root, name);
  if (result[key(file)]) return result[key(file)];
  const text = readFileSync(file, 'utf8');
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true);
  const entry = {imports: [], values: [], types: [], star: false, typeStar: false,
    errors: [], duplicates: []};
  let runtimeSource = null;
  result[key(file)] = entry;
  for (const error of source.parseDiagnostics) {
    const position = source.getLineAndCharacterOfPosition(error.start ?? 0);
    entry.errors.push(`${name}:${position.line + 1}:${position.character + 1} ${ts.flattenDiagnosticMessageText(error.messageText, '\n')}`);
  }
  if (!source.isDeclarationFile && !entry.errors.length) {
    const emitted = ts.transpileModule(text, {fileName: file, reportDiagnostics: true,
      compilerOptions: {target: ts.ScriptTarget.ESNext, module: ts.ModuleKind.ESNext}});
    runtimeSource = ts.createSourceFile(file + '.js', emitted.outputText, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
    try {
      // 使用同一 Node 的模块语法解析；不链接、不求值、不逐文件启动子进程。
      new SourceTextModule(emitted.outputText, {identifier: file});
    } catch (error) {
      entry.errors.push(`${name}: ${String(error.message).slice(0, 900)}`);
    }
  }
  const localTypes = new Set(source.statements.filter(statement =>
    ts.isInterfaceDeclaration(statement) || ts.isTypeAliasDeclaration(statement))
    .map(statement => statement.name.text));
  for (const statement of source.statements) {
    if (ts.isImportDeclaration(statement) && ts.isStringLiteral(statement.moduleSpecifier)) {
      const clause = statement.importClause, bindings = clause?.namedBindings;
      if (bindings && ts.isNamedImports(bindings)) {
        const target = statement.moduleSpecifier.text;
        if (target.startsWith('.')) entry.imports.push({target,
          resolved: ts.resolveModuleName(target, file, options, ts.sys).resolvedModule?.resolvedFileName ?? null,
          names: bindings.elements.map(value => ({name: value.propertyName?.text ?? value.name.text,
            typeOnly: Boolean(source.isDeclarationFile || clause.isTypeOnly || value.isTypeOnly)}))});
      }
    }
    if (ts.isExportDeclaration(statement)) {
      if (!statement.exportClause) {
        if (statement.isTypeOnly) entry.typeStar = true;
        else entry.star = true;
      } else if (ts.isNamedExports(statement.exportClause)) {
        for (const element of statement.exportClause.elements) {
          const localType = !statement.moduleSpecifier && localTypes.has(element.propertyName?.text ?? element.name.text);
          (statement.isTypeOnly || element.isTypeOnly || localType ? entry.types : entry.values).push(element.name.text);
        }
      } else entry.values.push(statement.exportClause.name.text);
      continue;
    }
    if (!modifier(statement, ts.SyntaxKind.ExportKeyword)) continue;
    const declared = source.isDeclarationFile || modifier(statement, ts.SyntaxKind.DeclareKeyword);
    const names = ts.isVariableStatement(statement)
      ? statement.declarationList.declarations.flatMap(value => bindingNames(value.name))
      : statement.name ? [statement.name.text] : [];
    if (ts.isInterfaceDeclaration(statement) || ts.isTypeAliasDeclaration(statement) || declared)
      entry.types.push(...names);
    else {
      entry.values.push(...names);
      if (ts.isClassDeclaration(statement) || ts.isEnumDeclaration(statement)) entry.types.push(...names);
    }
  }
  // 值导出以实际类型擦除后的模块为准，局部 interface 的 export 列表不能变成运行值。
  if (runtimeSource) {
    entry.values = [];
    entry.star = false;
    for (const statement of runtimeSource.statements) {
      if (ts.isExportAssignment(statement)) entry.values.push('default');
      else if (ts.isExportDeclaration(statement)) {
        if (!statement.exportClause) entry.star = true;
        else if (ts.isNamedExports(statement.exportClause))
          entry.values.push(...statement.exportClause.elements.map(element => element.name.text));
        else entry.values.push(statement.exportClause.name.text);
      } else if (modifier(statement, ts.SyntaxKind.ExportKeyword)) {
        if (modifier(statement, ts.SyntaxKind.DefaultKeyword)) entry.values.push('default');
        else if (ts.isVariableStatement(statement))
          entry.values.push(...statement.declarationList.declarations.flatMap(value => bindingNames(value.name)));
        else if (statement.name) entry.values.push(statement.name.text);
      }
    }
  }
  function visit(node) {
    if (ts.isObjectLiteralExpression(node)) {
      const seen = new Map();
      for (const property of node.properties) {
        const name = property.name;
        if (!name || ts.isComputedPropertyName(name) ||
            !(ts.isIdentifier(name) || ts.isStringLiteral(name) || ts.isNumericLiteral(name))) continue;
        const kind = ts.isGetAccessorDeclaration(property) ? 'get'
          : ts.isSetAccessorDeclaration(property) ? 'set' : 'init';
        const kinds = seen.get(name.text) ?? [];
        if (kinds.includes(kind) || (kind === 'init' && kinds.length) ||
            (kind !== 'init' && kinds.includes('init')))
          entry.duplicates.push(`${name.text}（第 ${source.getLineAndCharacterOfPosition(property.getStart(source)).line + 1} 行）`);
        seen.set(name.text, [...kinds, kind]);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  if (scanImports) for (const dependency of entry.imports)
    if (dependency.resolved && /\.(?:ts|mts|cts)$/.test(dependency.resolved)) inspect(dependency.resolved, false);
  return entry;
}
for (const name of requested) inspect(name, true);
process.stdout.write(JSON.stringify(result));
"""


def module_details(repo: Path, names: list[str]) -> dict:
    if not names:
        return {}
    for name in names:
        source_module(repo / name)
    compiler = config.parser_path("typescript_parser", repo)
    if compiler is None:
        raise ValueError("TS 源检查须配置 merge.typescript_parser（项目 TypeScript 编译器路径），不能跳过")
    if not compiler.is_file():
        raise ValueError(f"TypeScript 编译器不存在：{compiler}")
    node = shutil.which("node")
    if not node:
        raise ValueError("找不到 node，无法检查 TypeScript 模块")
    with tempfile.TemporaryDirectory() as temp:
        script = Path(temp) / "typescript-modules.mjs"
        script.write_text(_SCRIPT, encoding="utf-8")
        process = subprocess.run([node, '--experimental-vm-modules', str(script), str(compiler), str(repo), *sorted(set(names))],
                                 cwd=repo, text=True, capture_output=True, encoding="utf-8")
    if process.returncode:
        raise ValueError(f"TypeScript 模块解析失败：{process.stderr.strip()[:1200]}")
    return json.loads(process.stdout)
