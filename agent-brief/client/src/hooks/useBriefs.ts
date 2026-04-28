/*
 * useBriefs — sources engine-published Briefs at build time.
 *
 * The orchestrator (../../../../src/publish.py) writes
 * data/briefs.json on every publish; vite build snapshots its contents
 * into the SPA bundle. There is no runtime fetch — the deploy is
 * fully static (GitHub Pages + custom domain).
 *
 * Falls back to the design fixtures in data/content.ts when
 * briefs.json contains no entries — keeps the UI shaped during
 * pre-deploy preview / first-publish state.
 */

import publishedBriefsRaw from "../../../../data/briefs.json";
import { briefs as designFixtures, type Brief } from "@/data/content";

// Cast: briefs.json is schema-validated by the engine on every write
// (src/publish._validate_briefs_file) and on every publish boundary.
// TypeScript's structural inference of the JSON literal is too narrow
// for the live `Brief` type (e.g., the union-typed `tags`); the runtime
// contract is `Brief[]`.
const publishedBriefs = publishedBriefsRaw as unknown as Brief[];

export function useBriefs(): { briefs: Brief[]; loading: boolean } {
  return {
    briefs: publishedBriefs.length > 0 ? publishedBriefs : designFixtures,
    loading: false,
  };
}
