// The Devices tab of an Apple app: the latest picture of each device, light and dark, as Forge's
// checks took them on the server's Macs (`.forge/out/apple/`).

import { RefreshCw, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { DeviceScreen } from "../api/apple";
import type { ProjectApi } from "../api/project";
import { t } from "../lib/i18n";

const DEVICES: [DeviceScreen["platform"], string][] = [
  ["ios", "iPhone"],
  ["ipados", "iPad"],
  ["macos", "Mac"],
  ["watchos", "Apple Watch"],
];

interface Props {
  api: ProjectApi;
  refreshKey: number; // a new turn may have taken new pictures
  onError: (message: string) => void;
  approvalPage?: string; // a link to the page with everything to check before approving
}

export function AppleScreens({ api, refreshKey, onError, approvalPage }: Props) {
  const [screens, setScreens] = useState<DeviceScreen[] | null>(null);
  const load = useCallback(async () => {
    try {
      setScreens(await api.screens());
    } catch (err) {
      onError((err as Error).message);
    }
  }, [api, onError]);
  useEffect(() => {
    void load();
  }, [load, refreshKey]);
  return (
    <div className="h-full space-y-4 overflow-y-auto p-3" data-testid="apple-screens">
      <div className="flex items-center">
        <h2 className="flex-1 text-sm font-medium">{t("devices")}</h2>
        {approvalPage && (
          <Link to={approvalPage} className="mr-2 inline-flex items-center gap-1 text-xs text-accent">
            <ShieldCheck className="size-4" />
            {t("readyForApproval")}
          </Link>
        )}
        <button type="button" title={t("reloadPreview")} className="p-1 text-muted" onClick={() => void load()}>
          <RefreshCw className="size-4" />
        </button>
      </div>
      {screens?.length === 0 && <p className="text-sm text-muted">{t("noScreens")}</p>}
      {DEVICES.map(([platform, label]) => {
        const shots = (screens ?? []).filter((s) => s.platform === platform);
        return shots.length ? <Device key={platform} label={label} shots={shots} /> : null;
      })}
    </div>
  );
}

function Device({ label, shots }: { label: string; shots: DeviceScreen[] }) {
  return (
    <section>
      <h3 className="mb-1 text-xs font-medium text-muted">{label}</h3>
      <div className="flex flex-wrap items-start gap-3">
        {[...shots]
          .sort((a, b) => Number(a.dark) - Number(b.dark))
          .map((shot) => {
            const mode = shot.dark ? t("dark") : t("light");
            return (
              <figure key={mode} className="max-w-[48%] min-w-32 flex-1">
                <img src={shot.url} alt={`${label} (${mode})`} className="max-h-96 w-auto rounded-md border border-line" />
                <figcaption className="mt-0.5 text-xs text-muted">{mode}</figcaption>
              </figure>
            );
          })}
      </div>
    </section>
  );
}
