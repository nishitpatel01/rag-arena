import React, { Suspense, lazy } from "react";
import type { RouteObject } from "react-router-dom";
import { OverviewPage } from "../pages/OverviewPage";
import { EmptyState } from "../components/ui/EmptyState";

const IngestPage = lazy(() => import("../features/ingest/IngestPage").then((m) => ({ default: m.IngestPage })));
const LevelPage = lazy(() => import("../features/levels/LevelPage").then((m) => ({ default: m.LevelPage })));
const ComparePage = lazy(() => import("../features/compare/ComparePage").then((m) => ({ default: m.ComparePage })));
const WorkbenchPage = lazy(() => import("../features/workbench/WorkbenchPage").then((m) => ({ default: m.WorkbenchPage })));
const ReleasesPage = lazy(() => import("../features/releases/ReleasesPage").then((m) => ({ default: m.ReleasesPage })));
const LeaderboardPage = lazy(() => import("../pages/LeaderboardPage").then((m) => ({ default: m.LeaderboardPage })));

const withSuspense = (node: React.ReactNode): React.ReactElement => (
  <Suspense fallback={<p className="py-6 text-xs text-ink-3">Loading view…</p>}>{node}</Suspense>
);

export const routes: RouteObject[] = [
  { path: "/", element: <OverviewPage /> },
  { path: "/ingest", element: withSuspense(<IngestPage />) },
  { path: "/workbench", element: withSuspense(<WorkbenchPage />) },
  { path: "/levels/:levelId", element: withSuspense(<LevelPage />) },
  { path: "/compare", element: withSuspense(<ComparePage />) },
  { path: "/leaderboard", element: withSuspense(<LeaderboardPage />) },
  { path: "/releases", element: withSuspense(<ReleasesPage />) },
  { path: "*", element: <EmptyState title="Not found" description="No page matches this URL." /> },
];
