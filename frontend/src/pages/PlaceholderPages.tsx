import React from "react";
import { Link } from "react-router-dom";
import { EmptyState } from "../components/ui/EmptyState";

export const ComparePage: React.FC = () => (
  <div className="flex flex-col gap-3">
    <h1 className="text-lg font-semibold text-ink-1">Compare</h1>
    <EmptyState
      title="Available after Level 2"
      description="Compare runs the enabled levels concurrently on one question and stacks their stage rails. It unlocks once two levels have passed their Definition of Done."
      action={
        <Link to="/" className="text-xs font-medium text-moss hover:underline">
          Back to the level ladder
        </Link>
      }
    />
  </div>
);

export const LeaderboardPage: React.FC = () => (
  <div className="flex flex-col gap-3">
    <h1 className="text-lg font-semibold text-ink-1">Leaderboard</h1>
    <EmptyState
      title="No measured runs yet"
      description="The leaderboard aggregates query_runs rows (live) or recordings of real runs (replay), each with provenance. It is populated in Phase 6; hand-typed metrics are not shown."
    />
  </div>
);
