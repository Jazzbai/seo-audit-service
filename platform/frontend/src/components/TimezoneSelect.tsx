import type { SelectHTMLAttributes } from 'react'
import fallbackTimezones from '../lib/timezones.json'

// The bundled Intl timezone list also supports older browsers without this API.
const timezoneIntl = Intl as typeof Intl & { supportedValuesOf?: (key: 'timeZone') => string[] }
const availableTimezones = timezoneIntl.supportedValuesOf?.('timeZone') ?? fallbackTimezones
const commonTimezones: Array<[string, string]> = [
  ['America/New_York', 'Eastern Time — New York'],
  ['America/Chicago', 'Central Time — Chicago / Houston'],
  ['America/Denver', 'Mountain Time — Denver'],
  ['America/Phoenix', 'Arizona — Phoenix'],
  ['America/Los_Angeles', 'Pacific Time — Los Angeles'],
  ['America/Anchorage', 'Alaska — Anchorage'],
  ['Pacific/Honolulu', 'Hawaii — Honolulu'],
  ['UTC', 'Coordinated Universal Time'],
]
const commonIds = new Set(commonTimezones.map(([id]) => id))
const timezoneGroups = new Map<string, string[]>()
for (const timezone of [...new Set(availableTimezones)].sort()) {
  if (commonIds.has(timezone)) continue
  const region = timezone.split('/')[0]
  timezoneGroups.set(region, [...(timezoneGroups.get(region) ?? []), timezone])
}
const knownIds = new Set([...availableTimezones, ...commonIds])

type Props = Omit<SelectHTMLAttributes<HTMLSelectElement>, 'value' | 'onChange'> & {
  value: string
  onChange: (value: string) => void
}

export function TimezoneSelect({ value, onChange, ...props }: Props) {
  return <select {...props} value={value} onChange={(event) => onChange(event.target.value)}>
    <option value="" disabled>Select a timezone</option>
    {value && !knownIds.has(value) && <optgroup label="Saved or detected timezone"><option value={value}>{value.replaceAll('_', ' ')}</option></optgroup>}
    <optgroup label="Common timezones">
      {commonTimezones.map(([id, label]) => <option key={id} value={id}>{label} ({id})</option>)}
    </optgroup>
    {[...timezoneGroups].map(([region, timezones]) => <optgroup key={region} label={region}>
      {timezones.map((id) => <option key={id} value={id}>{id.slice(region.length + 1).replaceAll('_', ' ')} ({id})</option>)}
    </optgroup>)}
  </select>
}
