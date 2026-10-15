import {marked} from 'marked';
import {memo, useMemo} from 'react';
import ReactMarkdown from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';

/**
 * Markdown rendered as React elements, never as an HTML string.
 *
 * Model replies, fetched pages, memory and artifacts all come through here, so
 * raw HTML in the source shows as text instead of reaching the DOM — `<br>` is
 * the one tag kept, as models use it for line breaks inside table cells. Link
 * URLs pass react-markdown's default filter, which drops `javascript:` and the
 * other unsafe protocols.
 *
 * Renders its nodes bare: the caller's element carries `data-md` and `prose`,
 * which style them (see `[data-md]` in base.css).
 */
export const Markdown = memo(function Markdown({text}: {text: string}) {
  return <ReactMarkdown remarkPlugins={remarkPlugins}>{text}</ReactMarkdown>;
});

/**
 * Markdown that grows a token at a time. Rendering the whole text on every
 * token is quadratic over a reply, so it is split into top-level blocks and a
 * block renders again only when its source changed — in practice, the last.
 *
 * A reference-style link resolves only within its own block here; the stored
 * message renders whole once the run ends.
 */
export function StreamingMarkdown({text}: {text: string}) {
  const blocks = useMemo(() => {
    const list = splitBlocks(text);
    if (list.length > 0) list[list.length - 1] = closeOpenBold(list[list.length - 1]);
    return list;
  }, [text]);
  return blocks.map((block, i) => <Markdown key={i} text={block} />);
}

/**
 * A bold run that has opened but not yet closed shows its `**` as literal
 * text until the closing pair lands, then snaps to bold — a flicker on every
 * emphasised phrase. Closing it provisionally renders it bold as it grows.
 * Only the growing last line is touched, and never inside code.
 */
function closeOpenBold(block: string): string {
  if (/^(```|~~~| {4})/.test(block)) return block;
  const nl = block.lastIndexOf('\n');
  const line = block.slice(nl + 1).replace(/`[^`]*`/g, '');
  if ((line.match(/\*\*/g) ?? []).length % 2 === 0) return block;
  // Just opened, nothing inside yet: hold the marker back rather than close
  // it on nothing.
  if (/\*\*\s*$/.test(block)) return block.replace(/\*\*\s*$/, '');
  return block.trimEnd() + '**';
}

function splitBlocks(text: string): string[] {
  return marked
    .lexer(text)
    .filter((token) => token.type !== 'space')
    .map((token) => token.raw);
}

interface MdNode {
  type: string;
  value?: string;
  children?: MdNode[];
}

const BR = /^<br\s*\/?>$/i;

/** Turns a raw `<br>` into a line break; every other raw tag stays text. */
function remarkBr() {
  const visit = (node: MdNode) => {
    for (const [i, child] of (node.children ?? []).entries()) {
      if (child.type === 'html' && BR.test(child.value ?? '')) {
        node.children![i] = {type: 'break'};
      } else {
        visit(child);
      }
    }
  };
  return visit;
}

const remarkPlugins = [remarkGfm, remarkBreaks, remarkBr];
