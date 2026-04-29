/*
 * useEpisodes — sources engine-published Episodes at build time.
 *
 * Mirrors useBriefs.ts. The podcast orchestrator
 * (../../../../src/podcast/episodes.py) writes data/episodes.json
 * after every hard publish gate passes. Vite snapshots the file's
 * contents into the SPA bundle at build time — there is no runtime
 * fetch.
 *
 * Falls back to the design fixtures in data/content.ts when
 * episodes.json contains no entries — keeps the UI shaped during
 * pre-deploy preview / first-publish state.
 */

import publishedEpisodesRaw from "../../../../data/episodes.json";
import { episodes as designFixtures, type Episode } from "@/data/content";

// Cast: episodes.json is Pydantic-validated by the engine on every
// write (src/podcast/episodes._write_episodes_json) and the publish
// gate G2 confirms each entry against the EpisodeRecord shape (which
// mirrors this Episode TS interface). TypeScript's structural inference
// of the JSON literal is too narrow for the live `Episode` type; the
// runtime contract is `Episode[]`.
const publishedEpisodes = publishedEpisodesRaw as unknown as Episode[];

export function useEpisodes(): { episodes: Episode[]; loading: boolean } {
  return {
    episodes: publishedEpisodes.length > 0 ? publishedEpisodes : designFixtures,
    loading: false,
  };
}
