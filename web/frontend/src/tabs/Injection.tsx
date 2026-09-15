import { extractTemplateVars, parseCountText } from '../injection'

interface Props {
  prompt: string
  countText: string
  cells: string[][]
  setCells: (update: (old: string[][]) => string[][]) => void
}

export default function Injection(p: Props) {
  const names = extractTemplateVars(p.prompt)
  const count = parseCountText(p.countText)
  return (
    <div className="card injection-card">
      <div className="log-head">
        <h3 className="injection-title">Injection</h3>
        <span className="hint">
          one row per generation · empty cell repeats the value above
          (first row falls back to the variable name)
        </span>
      </div>
      <div className="logwrap">
        <table className="log injection">
          <thead>
            <tr>{names.map((name) => <th key={name}>{name}</th>)}</tr>
          </thead>
          <tbody>
            {Array.from({ length: count }, (_, i) => (
              <tr key={i}>
                {names.map((name, j) => (
                  <td key={name}>
                    <input
                      value={p.cells[i]?.[j] ?? ''}
                      onChange={(e) => {
                        const value = e.target.value
                        p.setCells((old) => {
                          // Size the table to count × vars first: the cells
                          // state starts empty, so map over a sized copy.
                          const next = Array.from({ length: count }, (_, ri) =>
                            names.map((_, ci) => old[ri]?.[ci] ?? ''))
                          next[i][j] = value
                          return next
                        })
                      }}
                      aria-label={`generation ${i + 1} ${name}`}
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
