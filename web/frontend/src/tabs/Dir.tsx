interface Props {
  outputDir: string
  setOutputDir: (v: string) => void
  contextDir: string
  setContextDir: (v: string) => void
  memoryDir: string
  setMemoryDir: (v: string) => void
}

export default function Dir(p: Props) {
  const fields: [string, string, (v: string) => void, string][] = [
    ['Output dir', p.outputDir, p.setOutputDir,
      'Folder where generated images and log_image_generate.csv are saved.'],
    ['Context dir', p.contextDir, p.setContextDir,
      'Folder with .md/.txt files automatically added to the prompt as context.'],
    ['Memory dir', p.memoryDir, p.setMemoryDir,
      'Folder with reference images sent along with the prompt to guide generation.'],
  ]
  return (
    <div className="card">
      {fields.map(([label, value, setValue, hint]) => (
        <div key={label}>
          <label className="field-label" title={hint}>{label} ⓘ</label>
          <input type="text" value={value} onChange={(e) => setValue(e.target.value)} />
        </div>
      ))}
      <div className="hint">Web version: type or paste the folder path (no folder picker in browser).</div>
    </div>
  )
}
