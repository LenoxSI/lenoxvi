// The unsaved-changes helpers: what "changed" means for a draft against the
// saved value (norm, same), and the one-after-another card save behind the
// unsaved bar and the setup wizard (saveCards). App.jsx owns the drafts and
// the bar; this module is pure logic, so `node --test` covers all of it.

// "Changed" means the draft differs from the saved value: numbers compare as
// numbers, strings trimmed, unset as ''.
export const norm = v => (v == null ? '' : typeof v === 'string' ? v.trim() : v)
export const same = (a, b) => {
  const x = norm(a), y = norm(b)
  return x === y || (x !== '' && y !== '' && !isNaN(x) && !isNaN(y) && Number(x) === Number(y))
}

// Saves the listed cards one after another with their own save(), for the
// unsaved bar and the setup wizard. One summary at the end instead of each
// card's message flashing over the last; a failed card keeps its draft.
export async function saveCards(cards, units, keys) {
  const end = s => (/[.!?]$/.test(s) ? s : s + '.')
  let done = 0, restart = false
  const failed = [], notes = []
  for (const k of keys) {
    const said = []
    let ok = false
    try { ok = !!(await cards.current[k]?.save(text => { said.push(text) })) }
    catch (e) { said.push(e.message) }
    const label = units[k].label
    if (!ok) {
      const why = (said[said.length - 1] || '').replace(/^[^:]*failed: /, '')
      failed.push(end(`${label} failed${why ? `: ${why}` : ''}`))
      continue
    }
    done++
    for (const text of said) {
      // "Saved — …" folds into the summary; anything else is a next step
      // the card asked for, kept in full.
      if (!text.startsWith('Saved')) notes.push(end(`${label}: ${text}`))
      else if (/restart/i.test(text) && !/no restart/i.test(text)) restart = true
    }
  }
  const head = failed.length ? `Saved ${done} of ${keys.length}. ${failed.join(' ')}`
    : `Saved.${restart ? ' Restart to apply.' : ''}`
  return { ok: !failed.length, notes, text: [head, ...notes].join(' ') }
}
