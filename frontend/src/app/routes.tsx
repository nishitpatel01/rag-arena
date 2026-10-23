import type { RouteObject } from "react-router-dom";
import { IngestPage } from "../features/ingest/IngestPage";
import { LevelPage } from "../features/levels/LevelPage";
import { OverviewPage } from "../pages/OverviewPage";
import { ComparePage } from "../features/compare/ComparePage";
import { LeaderboardPage } from "../pages/PlaceholderPages";
import { EmptyState } from "../components/ui/EmptyState";

export const routes: RouteObject[] = [
  { path: "/", element: <OverviewPage /> },
  { path: "/ingest", element: <IngestPage /> },
  { path: "/levels/:levelId", element: <LevelPage /> },
  { path: "/compare", element: <ComparePage /> },
  { path: "/leaderboard", element: <LeaderboardPage /> },
  { path: "*", element: <EmptyState title="Not found" description="No page matches this URL." /> },
];
