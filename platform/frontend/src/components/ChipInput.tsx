import { useId, useRef, useState, type ClipboardEvent, type KeyboardEvent } from 'react'
import { Plus, X } from 'lucide-react'

// Parse newly entered values only: existing items may legitimately contain commas.
function parseItems(text: string) {
  const items: string[] = []
  let item = ''
  let quoted = false
  const flush = () => {
    if (item.trim()) items.push(item.trim())
    item = ''
  }
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index]
    if (character === '"' && quoted) {
      if (text[index + 1] === '"') { item += '"'; index += 1 }
      else quoted = false
    } else if (character === '"' && !item.trim()) {
      quoted = true
    } else if (!quoted && /[,\r\n]/.test(character)) {
      flush()
    } else {
      item += character
    }
  }
  flush()
  return { items, quoted }
}

export function ChipInput({ label, values, onChange, placeholder, hint, disabled = false }: {
  label: string
  values: string[]
  onChange: (values: string[]) => void
  placeholder: string
  hint?: string
  disabled?: boolean
}) {
  const [draft, setDraft] = useState('')
  const input = useRef<HTMLTextAreaElement>(null)
  const id = useId()

  function add(text: string) {
    if (disabled) return
    const next = [...new Set([...values, ...parseItems(text).items])]
    if (next.length !== values.length) onChange(next)
    setDraft('')
  }

  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (disabled || event.nativeEvent.isComposing) return
    if ((event.key === 'Enter' && !event.shiftKey) || (event.key === ',' && !parseItems(draft).quoted)) {
      event.preventDefault()
      add(draft)
    }
    if (event.key === 'Backspace' && !draft && values.length) onChange(values.slice(0, -1))
  }

  function paste(event: ClipboardEvent<HTMLTextAreaElement>) {
    if (disabled) return
    const text = event.clipboardData.getData('text/plain')
    if (!/[,\r\n]/.test(text)) return
    event.preventDefault()
    const { selectionStart, selectionEnd } = event.currentTarget
    add(draft.slice(0, selectionStart) + text + draft.slice(selectionEnd))
  }

  return <div className="field">
    <label className="field-label" htmlFor={id}>{label}</label>
    <div className="chip-input">
      {values.map((value) => <span className="chip" key={value}>{value}<button type="button" aria-label={`Remove ${value}`} disabled={disabled} onMouseDown={(event) => event.preventDefault()} onClick={() => onChange(values.filter((item) => item !== value))}><X size={12} /></button></span>)}
      <textarea id={id} ref={input} aria-describedby={`${id}-hint`} rows={1} value={draft} disabled={disabled} onChange={(event) => setDraft(event.target.value)} onKeyDown={keyDown} onPaste={paste} onBlur={(event) => add(event.currentTarget.value)} placeholder={values.length ? 'Add another' : placeholder} />
      <button type="button" className="icon-button" aria-label={`Add ${label.toLowerCase()}`} disabled={disabled} onMouseDown={(event) => event.preventDefault()} onClick={() => { add(draft); input.current?.focus() }}><Plus size={14} /></button>
    </div>
    <span className="field-hint" id={`${id}-hint`}>{hint && <>{hint} </>}Separate items with commas or new lines. Press Enter or + to add. Use quotes for a single item like "Houston, TX".</span>
  </div>
}
