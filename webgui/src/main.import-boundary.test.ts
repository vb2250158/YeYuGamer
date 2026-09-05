import { existsSync, readFileSync } from 'node:fs'
import { dirname, extname, normalize, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

const sourceRoot = dirname(fileURLToPath(import.meta.url))
const mainPath = resolve(sourceRoot, 'main.ts')
const appPath = resolve(sourceRoot, 'App.vue')
const routerPath = resolve(sourceRoot, 'router.ts')

function isRuntimeImport(node: ts.ImportDeclaration): boolean {
  const clause = node.importClause
  if (!clause) return true
  if (clause.isTypeOnly) return false
  if (clause.name) return true
  if (clause.namedBindings && ts.isNamedImports(clause.namedBindings)) {
    return clause.namedBindings.elements.some((element) => !element.isTypeOnly)
  }
  return true
}

function resolveRelativeImport(importer: string, specifier: string): string | undefined {
  if (!specifier.startsWith('.')) return undefined
  const unresolved = resolve(dirname(importer), specifier)
  const candidates = extname(unresolved)
    ? [unresolved]
    : [
        `${unresolved}.ts`,
        `${unresolved}.tsx`,
        `${unresolved}.js`,
        `${unresolved}.vue`,
        resolve(unresolved, 'index.ts'),
      ]
  return candidates.find((candidate) => existsSync(candidate))
}

function runtimeStaticImports(filePath: string): string[] {
  if (!['.ts', '.tsx', '.js'].includes(extname(filePath))) return []
  const source = readFileSync(filePath, 'utf8')
  const sourceFile = ts.createSourceFile(
    filePath,
    source,
    ts.ScriptTarget.Latest,
    true,
    filePath.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  )
  return sourceFile.statements
    .filter(ts.isImportDeclaration)
    .filter(isRuntimeImport)
    .map((node) => (node.moduleSpecifier as ts.StringLiteral).text)
}

function runtimeStaticClosure(entry: string): Set<string> {
  const visited = new Set<string>()
  const pending = [entry]
  while (pending.length > 0) {
    const current = pending.pop()
    if (!current) continue
    const canonical = normalize(current)
    if (visited.has(canonical)) continue
    visited.add(canonical)
    for (const specifier of runtimeStaticImports(current)) {
      const dependency = resolveRelativeImport(current, specifier)
      if (dependency) pending.push(dependency)
    }
  }
  return visited
}

function callsCreateWebHistory(filePath: string): boolean {
  const source = readFileSync(filePath, 'utf8')
  const sourceFile = ts.createSourceFile(
    filePath,
    source,
    ts.ScriptTarget.Latest,
    true,
    filePath.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  )
  let found = false
  function visit(node: ts.Node): void {
    if (
      ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === 'createWebHistory'
    ) {
      found = true
    }
    ts.forEachChild(node, visit)
  }
  visit(sourceFile)
  return found
}

describe('WebGUI bootstrap import boundary', () => {
  it('keeps App, router, and createWebHistory outside the pre-bootstrap static graph', () => {
    const closure = runtimeStaticClosure(mainPath)

    expect(closure).not.toContain(normalize(appPath))
    expect(closure).not.toContain(normalize(routerPath))

    for (const filePath of closure) {
      if (!['.ts', '.tsx', '.js'].includes(extname(filePath))) continue
      expect(runtimeStaticImports(filePath), filePath).not.toContain('vue-router')
      expect(callsCreateWebHistory(filePath), filePath).toBe(false)
    }
  })

  it('loads App and router dynamically after awaiting session bootstrap', () => {
    const source = readFileSync(mainPath, 'utf8')
    const sourceFile = ts.createSourceFile(
      mainPath,
      source,
      ts.ScriptTarget.Latest,
      true,
      ts.ScriptKind.TS,
    )
    const dynamicImports = new Map<string, number>()
    let awaitedBootstrapEnd = -1

    function visit(node: ts.Node): void {
      if (
        ts.isAwaitExpression(node)
        && ts.isCallExpression(node.expression)
        && ts.isIdentifier(node.expression.expression)
        && node.expression.expression.text === 'bootstrapWebGuiSession'
      ) {
        awaitedBootstrapEnd = node.end
      }
      if (
        ts.isCallExpression(node)
        && node.expression.kind === ts.SyntaxKind.ImportKeyword
        && node.arguments.length === 1
        && ts.isStringLiteral(node.arguments[0])
      ) {
        dynamicImports.set(node.arguments[0].text, node.pos)
      }
      ts.forEachChild(node, visit)
    }
    visit(sourceFile)

    expect(awaitedBootstrapEnd).toBeGreaterThan(0)
    expect(dynamicImports.get('./App.vue')).toBeGreaterThan(awaitedBootstrapEnd)
    expect(dynamicImports.get('./router')).toBeGreaterThan(awaitedBootstrapEnd)
  })
})
