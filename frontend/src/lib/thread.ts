/* ── Thread helpers shared by the message list and the spine rail ──────
   The spine navigates the thread, so it needs the same two facts the
   thread does: where a message lives in the DOM, and what it says in one
   line. Both live here so the rail and the bubbles cannot disagree. */

/** DOM id of a message's turn — the spine's jump target. */
export function messageAnchorId(id: string): string {
  return `msg-${id}`;
}

/**
 * A user message can be a JSON array of multimodal parts rather than plain
 * text (the same shape `MessageBubble` renders). Flatten it to the text parts
 * and note the attachments, so the rail says "2 images" instead of a wall of
 * base64.
 */
function plainTextOf(content: string): string {
  let parts: unknown;
  try {
    parts = JSON.parse(content);
  } catch {
    return content;
  }
  if (!Array.isArray(parts) || parts.length === 0 || !(parts[0] as {type?: string})?.type) {
    return content;
  }
  const typed = parts as {type: string; text?: string}[];
  const text = typed
    .filter((p) => p.type === 'text' && p.text)
    .map((p) => p.text!)
    .join('\n')
    .trim();
  const attachments = typed.length - typed.filter((p) => p.type === 'text').length;
  if (text) return attachments > 0 ? `${text} (+${attachments} attached)` : text;
  return attachments > 0 ? `${attachments} attachment${attachments === 1 ? '' : 's'}` : '';
}

/**
 * One line of gist for a message. Markdown is *stripped*, not rendered: the
 * rail is a navigation aid, and `### ` or `**` in a 40-character label is
 * noise that costs characters the gist needs.
 */
export function messageExcerpt(content: string, max = 220): string {
  const text = plainTextOf(content)
    // Fenced code says nothing useful in one line; name it instead.
    .replace(/```[\s\S]*?```/g, ' [code] ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' [image] ')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')
    .replace(/^\s{0,3}>\s?/gm, '')
    .replace(/^\s{0,3}[-*+]\s+/gm, '· ')
    .replace(/(\*\*|__|\*|_|~~)/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (text.length <= max) return text;
  // Prefer a word boundary, but never give back less than most of the budget.
  const cut = text.slice(0, max);
  const space = cut.lastIndexOf(' ');
  return `${(space > max * 0.6 ? cut.slice(0, space) : cut).trimEnd()}…`;
}
