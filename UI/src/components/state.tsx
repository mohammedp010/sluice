import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Info, TriangleAlert, Unplug } from "lucide-react";
import { ApiError } from "../lib/api";

export const START_CMD = "SLUICE_POLICY=policies/lab.yaml .venv/bin/uvicorn sluice.api.app:app_factory --factory --port 8000";

/** Fetch once, and again every `every` ms when given. Keeps the last good value on error. */
export function usePoll<T>(fn: () => Promise<T>, every?: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const load = useCallback(async () => {
    try {
      setData(await fnRef.current());
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e : new ApiError(String(e), "http"));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    setLoading(true);
    load();
    if (!every) return;
    const t = setInterval(() => document.visibilityState === "visible" && load(), every);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, every, ...deps]);
  return { data, error, loading, reload: load };
}

/** Explains a failed read: gateway down, admin API off, or an HTTP error. */
export function ApiProblem({ error }: { error: ApiError }) {
  if (error.kind === "offline") {
    return (
      <div className="notice bad" role="alert">
        <Unplug aria-hidden />
        <div>
          <h3>Can't reach the gateway on :8000</h3>
          <p>Start it with the lab policy from the repo root, then this view reconnects on its own:</p>
          <pre className="cmd">{START_CMD}</pre>
        </div>
      </div>
    );
  }
  if (error.kind === "admin_off") {
    return (
      <div className="notice warn" role="alert">
        <TriangleAlert aria-hidden />
        <div>
          <h3>The gateway is up, but its admin API is off</h3>
          <p>
            Set <code>admin: {"{enabled: true}"}</code> in the policy you're running, or restart with
            the lab policy, which turns it on:
          </p>
          <pre className="cmd">{START_CMD}</pre>
        </div>
      </div>
    );
  }
  return (
    <div className="notice bad" role="alert">
      <TriangleAlert aria-hidden />
      <div>
        <h3>The gateway answered {error.message}</h3>
        <p>Check its log for the traceback, then reload this view.</p>
      </div>
    </div>
  );
}

export function Empty({ icon, title, children }: { icon?: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <div>
        {icon ?? <Info aria-hidden />}
        <h3>{title}</h3>
        {children && <p>{children}</p>}
      </div>
    </div>
  );
}

export function SkeletonRows({ n = 6 }: { n?: number }) {
  return (
    <div style={{ padding: 12, display: "grid", gap: 10 }} aria-busy="true" aria-label="Loading">
      {Array.from({ length: n }, (_, i) => (
        <div key={i} className="skel" style={{ height: 12, width: `${90 - ((i * 17) % 40)}%` }} />
      ))}
    </div>
  );
}
