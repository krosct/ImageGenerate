import { useEffect, useRef } from 'react'

export interface MenuItem { label: string; onClick?: () => void; disabled?: boolean; separator?: boolean }

// Right-click menu: closes on a click outside, on Esc and after choosing.
export default function ContextMenu({ x, y, items, onClose }: {
  x: number; y: number; items: MenuItem[]; onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const down = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) onClose() }
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('mousedown', down)
    window.addEventListener('keydown', key)
    window.addEventListener('scroll', onClose, true)
    return () => {
      window.removeEventListener('mousedown', down)
      window.removeEventListener('keydown', key)
      window.removeEventListener('scroll', onClose, true)
    }
  }, [onClose])
  const left = Math.min(x, window.innerWidth - 240)
  const top = Math.min(y, window.innerHeight - 28 * items.length - 16)
  return (
    <div ref={ref} className="ctx-menu" style={{ left, top }} role="menu">
      {items.map((item, i) => item.separator
        ? <div key={i} className="ctx-sep" />
        : (
          <button key={i} className="ctx-item" disabled={item.disabled} role="menuitem"
            onClick={() => { onClose(); item.onClick?.() }}>
            {item.label}
          </button>
        ))}
    </div>
  )
}
