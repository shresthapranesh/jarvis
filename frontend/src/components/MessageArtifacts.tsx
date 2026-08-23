import type {ArtifactCard} from '../lib/types';

/** Icon per artifact kind — mirrors ArtifactPanel's `renderArtifactBody` switch. */
function KindIcon({kind}: {kind: string}) {
  const common = {
    width: 14,
    height: 14,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 2,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
  };
  if (kind === 'audio') {
    return (
      <svg {...common}>
        <path d="M9 18V5l12-2v13" />
        <circle cx="6" cy="18" r="3" />
        <circle cx="18" cy="16" r="3" />
      </svg>
    );
  }
  if (kind === 'video') {
    return (
      <svg {...common}>
        <polygon points="23 7 16 12 23 17 23 7" />
        <rect x="1" y="5" width="15" height="14" rx="2" ry="2" />
      </svg>
    );
  }
  if (kind === 'image') {
    return (
      <svg {...common}>
        <rect x="3" y="3" width="18" height="18" rx="2" />
        <circle cx="8.5" cy="8.5" r="1.5" />
        <polyline points="21 15 16 10 5 21" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      <line x1="16" y1="13" x2="8" y2="13" />
      <line x1="16" y1="17" x2="8" y2="17" />
    </svg>
  );
}

function subtitle(a: ArtifactCard): string {
  const bits = [a.kind === 'binary' ? (a.mimeType ?? 'file') : a.kind];
  if (a.action === 'updated') bits.push('updated');
  return bits.join(' · ');
}

interface Props {
  artifacts: ArtifactCard[];
  onOpen?: (id: string) => void;
  /** Marks the card the side panel is currently showing. */
  selectedId?: string | null;
}

/**
 * The artifacts one assistant turn produced, rendered under that turn.
 *
 * This is the only entry point to the artifact side panel — clicking a card
 * opens the panel on the full conversation list with that artifact selected,
 * which is why the whole card is a button rather than a label with an
 * affordance tucked in a corner.
 */
export function MessageArtifacts({artifacts, onOpen, selectedId}: Props) {
  if (artifacts.length === 0) return null;
  return (
    <div className="message-artifacts">
      {artifacts.map((a) => (
        <button
          key={a.id}
          type="button"
          className={`artifact-card${a.id === selectedId ? ' artifact-card--active' : ''}`}
          onClick={() => onOpen?.(a.id)}
          title={`Open "${a.title}"`}
        >
          <span className="artifact-card-icon">
            <KindIcon kind={a.kind} />
          </span>
          <span className="artifact-card-text">
            <span className="artifact-card-title">{a.title}</span>
            <span className="artifact-card-meta">{subtitle(a)}</span>
          </span>
        </button>
      ))}
    </div>
  );
}
