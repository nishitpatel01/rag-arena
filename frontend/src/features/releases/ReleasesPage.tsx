import React, { useEffect, useMemo, useState } from "react";
import { ArrowRight, ExternalLink, Search } from "lucide-react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { useAppData } from "../../app/AppDataProvider";
import { useSafeMode } from "../../app/SafeMode";
import { Button } from "../../components/ui/Button";
import { EmptyState } from "../../components/ui/EmptyState";
import { Panel } from "../../components/ui/Panel";
import { Pill } from "../../components/ui/Pill";
import { SegmentedControl, type SegmentedOption } from "../../components/ui/SegmentedControl";
import { api } from "../../lib/api";
import { maskDeep } from "../../lib/safeMode";
import type { ReleaseDocument, ReleaseEntry, ReleasesResponse } from "../../types/arena";
import { DocMarkdown, renderRichInline } from "./DocMarkdown";
import { filterReleases, parseReleasesTab, visibleRouteLinks, type ReleasesTab } from "./releasesModel";

const TAB_OPTIONS: readonly SegmentedOption<ReleasesTab>[] = [
  { value: "timeline", label: "Release Timeline" },
  { value: "architecture", label: "System Architecture" },
  { value: "setup_guide", label: "Setup Guide" },
];

const ReleaseItemList: React.FC<{ readonly items: readonly string[]; readonly prefix: string }> = ({ items, prefix }) => {
  const { mask } = useSafeMode();
  return (
    <ul className="flex list-disc flex-col gap-2 pl-5 text-sm leading-relaxed text-ink-2">
      {items.map((raw, idx) => {
        const lines = raw.split("\n");
        const head = lines[0] ?? "";
        const subItems = lines
          .slice(1)
          .map((l) => l.replace(/^\s*(?:[*-]|\d+[.)])\s+/, "").trim())
          .filter(Boolean);
        return (
          <li key={`${prefix}-${idx}`}>
            <span>{renderRichInline(head, `${prefix}-${idx}-h`, mask)}</span>
            {subItems.length > 0 && (
              <ul className="mt-1.5 flex list-disc flex-col gap-1 pl-5 text-xs leading-relaxed text-ink-2">
                {subItems.map((sub, sIdx) => (
                  <li key={`${prefix}-${idx}-s-${sIdx}`}>{renderRichInline(sub, `${prefix}-${idx}-s-${sIdx}`, mask)}</li>
                ))}
              </ul>
            )}
          </li>
        );
      })}
    </ul>
  );
};

const ReleaseCard: React.FC<{
  readonly entry: ReleaseEntry;
  readonly isDeployed: boolean;
  readonly isLatest: boolean;
  readonly demoMode: string;
  readonly enabledLevels: readonly string[];
}> = ({ entry, isDeployed, isLatest, demoMode, enabledLevels }) => {
  const { mask } = useSafeMode();
  const links = visibleRouteLinks(entry.route_links, demoMode, enabledLevels);
  return (
    <article id={`release-v${entry.version}`} className="scroll-mt-20">
      <Panel
        title={
          <span className="inline-flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm font-semibold text-ink-1">v{entry.version}</span>
            {entry.date && <span className="font-mono text-xs font-normal text-ink-3">{entry.date}</span>}
            {isLatest && <Pill tone="info">latest</Pill>}
            {isDeployed && <Pill tone="ok">running build</Pill>}
          </span>
        }
        actions={
          entry.compare_url ? (
            <a
              href={entry.compare_url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-xs font-medium text-moss hover:underline"
            >
              Diff <ExternalLink className="h-3 w-3" aria-hidden />
            </a>
          ) : undefined
        }
        bodyClassName="flex flex-col gap-3"
      >
        {entry.summary && (
          <p className="text-sm font-medium leading-relaxed text-ink-1">
            {renderRichInline(entry.summary, `sum-${entry.version}`, mask)}
          </p>
        )}
        {links.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5" aria-label={`Routes introduced or updated in v${entry.version}`}>
            <span className="text-xs text-ink-3">Open in app:</span>
            {links.map((lnk) => (
              <Link
                key={lnk.path}
                to={lnk.path}
                className="inline-flex items-center gap-1 rounded-md border border-line bg-surface-0 px-2 py-0.5 text-xs font-medium text-ink-1 transition-colors hover:bg-surface-2"
              >
                {lnk.label}
                <ArrowRight className="h-3 w-3 text-moss" aria-hidden />
              </Link>
            ))}
          </div>
        )}
        {entry.sections.map((sec, sIdx) => (
          <div key={`${entry.version}-sec-${sIdx}`} className="flex flex-col gap-1.5">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-3">{sec.heading}</h3>
            <ReleaseItemList items={sec.items} prefix={`rel-${entry.version}-${sIdx}`} />
          </div>
        ))}
      </Panel>
    </article>
  );
};

const DocumentReader: React.FC<{ readonly doc: ReleaseDocument }> = ({ doc }) => (
  <div className="grid gap-5 xl:grid-cols-[16rem_minmax(0,1fr)]">
    <aside aria-label={`${doc.title} sections`} className="xl:sticky xl:top-4 xl:self-start">
      <Panel title="On this page" subtitle={doc.source_path} bodyClassName="max-h-[70vh] overflow-y-auto">
        <ol className="flex flex-col gap-1 text-xs">
          {doc.toc.map((item) => (
            <li key={item.id} className={item.level === 3 ? "pl-3" : ""}>
              <a
                href={`#${item.id}`}
                className="block truncate rounded px-1.5 py-1 text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink-1"
                title={item.title}
              >
                {item.title}
              </a>
            </li>
          ))}
        </ol>
      </Panel>
    </aside>
    <Panel title={doc.title} subtitle={`Compiled from ${doc.source_path}`} bodyClassName="flex flex-col gap-3">
      <DocMarkdown markdown={doc.markdown} />
    </Panel>
  </div>
);

export const ReleasesPage: React.FC = () => {
  const { health, levels } = useAppData();
  const { enabled: safe } = useSafeMode();
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const activeTab = parseReleasesTab(location.search);

  const [rawData, setRawData] = useState<ReleasesResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState<string>("");

  const load = React.useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.releases();
      setRawData(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    document.title = "Releases & Documentation — The RAG Arena";
  }, []);

  useEffect(() => {
    if (!rawData || !location.hash) return;
    const id = location.hash.slice(1);
    const el = document.getElementById(id);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [rawData, activeTab, location.hash]);

  const data = useMemo(() => (rawData && safe ? maskDeep(rawData, health) : rawData), [rawData, safe, health]);
  const enabledLevelIds = useMemo(
    () => (health?.enabled_levels ?? levels.filter((l) => l.enabled).map((l) => l.level_id)),
    [health, levels],
  );
  const filteredReleases = useMemo(
    () => (data ? filterReleases(data.releases, searchQuery) : []),
    [data, searchQuery],
  );
  const activeDoc = useMemo(
    () => data?.documents.find((d) => d.id === activeTab) ?? null,
    [data, activeTab],
  );

  const onTabChange = (next: ReleasesTab): void => {
    const nextParams = new URLSearchParams(searchParams);
    if (next === "timeline") nextParams.delete("tab");
    else nextParams.set("tab", next);
    setSearchParams(nextParams, { replace: true });
  };

  const deployedVersion = health?.app_version ?? data?.app_version ?? "";
  const demoMode = String(health?.demo_mode ?? data?.demo_mode ?? "replay");

  return (
    <div className="flex flex-col gap-5" data-testid="releases-page">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-lg font-semibold text-ink-1">Releases &amp; Documentation</h1>
          <p className="text-sm text-ink-2">
            Version history, architecture specs, and setup guides compiled directly from repository Markdown and kept in sync on push via <code className="rounded bg-surface-2 px-1 font-mono text-xs">release-please</code>.
          </p>
        </div>
        <SegmentedControl
          label="Documentation section"
          value={activeTab}
          options={TAB_OPTIONS}
          onChange={onTabChange}
          size="md"
        />
      </div>

      {data && (
        <div className="flex flex-wrap items-center gap-2 text-xs" aria-label="Release telemetry summary">
          <Pill tone="ok" mono>v{deployedVersion}</Pill>
          <Pill tone="neutral">{data.stats.release_count} releases</Pill>
          <Pill tone="neutral">{data.stats.architecture_count} architectures</Pill>
          <Pill tone="neutral">{data.stats.total_recordings} golden recordings</Pill>
          <Pill tone="info" mono>CHANGELOG.md · ARCHITECTURE.md · SETUP_GUIDE.md</Pill>
        </div>
      )}

      {loading && <p className="text-xs text-ink-3">Loading release history &amp; documentation…</p>}

      {!loading && error && (
        <EmptyState
          title="Could not load release documentation"
          description={error}
          action={
            <Button size="sm" variant="secondary" onClick={() => void load()}>
              Retry
            </Button>
          }
        />
      )}

      {!loading && data && activeTab === "timeline" && (
        <div className="grid gap-5 xl:grid-cols-[15rem_minmax(0,1fr)]">
          <aside aria-label="Version jump list" className="flex flex-col gap-3 xl:sticky xl:top-4 xl:self-start">
            <Panel title="Filter releases" bodyClassName="flex flex-col gap-2.5">
              <div className="relative">
                <Search className="pointer-events-none absolute left-2.5 top-2 h-3.5 w-3.5 text-ink-3" aria-hidden />
                <input
                  id="release-search-input"
                  type="search"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  placeholder="Search versions or features…"
                  aria-label="Search releases"
                  className="w-full rounded-md border border-line bg-surface-0 py-1.5 pl-8 pr-2.5 text-xs text-ink-1 placeholder:text-ink-3 focus:border-moss focus:outline-none"
                />
              </div>
              <ol className="flex flex-col gap-1 text-xs">
                {data.releases.map((rel, idx) => (
                  <li key={rel.version}>
                    <a
                      href={`#release-v${rel.version}`}
                      className="flex items-center justify-between rounded px-2 py-1 text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink-1"
                    >
                      <span className="font-mono font-medium text-ink-1">v{rel.version}</span>
                      <span className="font-mono text-[11px] text-ink-3">
                        {idx === 0 ? "latest" : rel.date}
                      </span>
                    </a>
                  </li>
                ))}
              </ol>
            </Panel>
          </aside>

          <section aria-label="Release timeline" className="flex min-w-0 flex-col gap-4">
            {filteredReleases.length === 0 ? (
              <EmptyState
                title="No matching releases"
                description={`No release matched "${searchQuery}".`}
                action={
                  <Button size="sm" variant="secondary" onClick={() => setSearchQuery("")}>
                    Clear filter
                  </Button>
                }
              />
            ) : (
              filteredReleases.map((entry) => (
                <ReleaseCard
                  key={entry.version}
                  entry={entry}
                  isDeployed={entry.version === deployedVersion}
                  isLatest={entry.version === data.releases[0]?.version}
                  demoMode={demoMode}
                  enabledLevels={enabledLevelIds}
                />
              ))
            )}
          </section>
        </div>
      )}

      {!loading && data && activeTab !== "timeline" && activeDoc && <DocumentReader doc={activeDoc} />}
    </div>
  );
};
