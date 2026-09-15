export function extractTemplateVars(prompt: string): string[] {
  const seen = new Set<string>()
  const names: string[] = []
  for (const m of prompt.matchAll(/\{\{([^{}]*)\}\}/g)) {
    const name = m[1].trim()
    if (name && !seen.has(name)) {
      seen.add(name)
      names.push(name)
    }
  }
  return names
}

// Empty cell repeats the value above; first row falls back to the variable name.
export function resolveInjectionRows(cells: string[][], names: string[]): string[][] {
  const previous = names.map(() => '')
  return cells.map((row) =>
    names.map((name, j) => {
      const value = (row[j] ?? '').trim()
      const resolved = value || previous[j] || name
      previous[j] = resolved
      return resolved
    }),
  )
}

export function parseCountText(countText: string): number {
  return /^[0-9]+$/.test(countText.trim()) ? parseInt(countText.trim(), 10) : 1
}
