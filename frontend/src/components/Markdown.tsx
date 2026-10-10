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
  const blocks = useMemo(() => splitBlocks(text), [text]);
  return blocks.map((block, i) => <Markdown key={i} text={block} />);
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
