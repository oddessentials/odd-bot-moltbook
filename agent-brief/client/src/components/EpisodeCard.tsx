/*
 * EpisodeCard — Daily Dispatch design system
 *
 * Compact card for podcast episode lists. The card itself is presentational —
 * the parent decides what happens on click (navigate, switch the featured player, etc.).
 */

import { Play } from "lucide-react";
import type { Episode } from "@/data/content";
import { formatShortDate } from "@/data/content";

interface EpisodeCardProps {
  episode: Episode;
  onSelect?: (episode: Episode) => void;
  isActive?: boolean;
}

export function EpisodeCard({ episode, onSelect, isActive }: EpisodeCardProps) {
  const thumb = `https://i.ytimg.com/vi/${episode.youtubeId}/hqdefault.jpg`;
  return (
    <button
      type="button"
      onClick={() => onSelect?.(episode)}
      className={
        "group flex h-full w-full flex-col overflow-hidden rounded-lg border bg-card text-left transition-all duration-200 hover:-translate-y-0.5 hover:shadow-[0_8px_30px_-12px_rgba(0,0,0,0.12)] " +
        (isActive
          ? "border-coral/60 ring-2 ring-coral/25"
          : "border-border hover:border-foreground/20")
      }
      aria-pressed={isActive}
    >
      <div className="relative aspect-video w-full overflow-hidden bg-muted">
        <img
          src={thumb}
          alt=""
          loading="lazy"
          className="h-full w-full object-cover transition-transform duration-500 group-hover:scale-[1.03]"
        />
        <div className="absolute inset-0 bg-gradient-to-t from-black/45 via-black/0 to-black/0" />
        <div className="absolute bottom-3 left-3 inline-flex items-center gap-2 rounded-full bg-background/90 px-2.5 py-1 text-[11px] font-mono font-medium text-foreground shadow">
          <Play className="h-3 w-3 fill-coral text-coral" />
          {episode.durationMinutes} min
        </div>
      </div>
      <div className="flex flex-1 flex-col p-5">
        <div className="flex items-center justify-between">
          <span className="kicker-coral">EP. {String(episode.episodeNo).padStart(2, "0")}</span>
          <span className="kicker">{formatShortDate(episode.date)}</span>
        </div>
        <h3
          className="mt-3 font-display text-lg font-semibold leading-snug tracking-tight text-card-foreground"
          style={{ fontVariationSettings: '"opsz" 96' }}
        >
          {episode.title}
        </h3>
        <p className="mt-2 line-clamp-2 text-sm leading-relaxed text-muted-foreground">
          {episode.description}
        </p>
        <p className="mt-auto pt-4 text-xs text-muted-foreground">
          With {episode.hosts.join(" & ")}
        </p>
      </div>
    </button>
  );
}

export default EpisodeCard;
