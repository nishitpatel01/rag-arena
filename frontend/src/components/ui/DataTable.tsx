import React from "react";
import { maskNode, useSafeMode } from "../../app/SafeMode";

export interface DataTableColumn<Row> {
  readonly key: string;
  readonly header: React.ReactNode;
  readonly render: (row: Row) => React.ReactNode;
  readonly align?: "left" | "right";
  readonly mono?: boolean;
}

export interface DataTableProps<Row> {
  readonly caption: string;
  readonly columns: readonly DataTableColumn<Row>[];
  readonly rows: readonly Row[];
  readonly rowKey: (row: Row, index: number) => string;
  readonly emptyText?: string;
  readonly dense?: boolean;
}

export function DataTable<Row>({
  caption,
  columns,
  rows,
  rowKey,
  emptyText = "No rows.",
  dense = true,
}: DataTableProps<Row>): React.ReactElement {
  const cell = dense ? "px-2 py-1" : "px-3 py-2";
  const { mask } = useSafeMode();
  return (
    <div className="w-full overflow-x-auto rounded-md border border-line">
      <table className="w-full border-collapse text-xs">
        <caption className="sr-only">{caption}</caption>
        <thead className="bg-surface-2 text-ink-3">
          <tr>
            {columns.map((col) => (
              <th
                key={col.key}
                scope="col"
                className={`${cell} whitespace-nowrap text-left font-semibold ${
                  col.align === "right" ? "text-right" : ""
                }`}
              >
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className={`${cell} text-ink-3`}>
                {emptyText}
              </td>
            </tr>
          ) : (
            rows.map((row, i) => (
              <tr key={rowKey(row, i)} className="border-t border-line align-top">
                {columns.map((col) => (
                  <td
                    key={col.key}
                    className={`${cell} text-ink-1 ${col.mono ? "font-mono" : ""} ${
                      col.align === "right" ? "text-right tabular-nums" : ""
                    }`}
                  >
                    {maskNode(col.render(row), mask)}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
