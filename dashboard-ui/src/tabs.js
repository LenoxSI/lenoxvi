// The dashboard's tabs, in nav order: App.jsx renders and routes them, and
// the setup wizard reads their ids to tell a tab hash from a card hash.
export const TABS = [
  { id: 'rooms', label: 'Rooms' },
  { id: 'voice', label: 'Voice' },
  { id: 'system', label: 'System' },
]

export const TAB_IDS = TABS.map(t => t.id)
