import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DeviceScreen } from "../api/apple";
import type { ProjectApi } from "../api/project";
import { AppleScreens } from "./AppleScreens";
import { ProjectPanel } from "./ProjectPanel";

afterEach(cleanup);

const PNG = "data:image/png;base64,iVBORw0KGgo=";
const shots: DeviceScreen[] = [
  { platform: "watchos", dark: true, url: `${PNG}#watch` },
  { platform: "ios", dark: true, url: `${PNG}#ios-dark` },
  { platform: "ios", dark: false, url: `${PNG}#ios-light` },
];

function fakeApi(found: DeviceScreen[] = shots) {
  const api = {
    screens: vi.fn(async () => found),
    list: vi.fn(async (path: string) => ({ path, entries: [] })),
  };
  return api as unknown as ProjectApi & { screens: ReturnType<typeof vi.fn> };
}

describe("AppleScreens", () => {
  it("shows each device's pictures, light before dark, in device order", async () => {
    const api = fakeApi();
    render(<AppleScreens api={api} refreshKey={0} onError={vi.fn()} />);
    await screen.findByAltText(/^iPhone \((Light|Hell)\)$/);
    const pictures = screen.getAllByRole("img").map((img) => img.getAttribute("src"));
    expect(pictures).toEqual([`${PNG}#ios-light`, `${PNG}#ios-dark`, `${PNG}#watch`]);
    expect(screen.queryByText("iPad")).toBeNull(); // no pictures of it yet
    fireEvent.click(screen.getByTitle(/^(Reload|Neu laden)$/));
    await waitFor(() => expect(api.screens).toHaveBeenCalledTimes(2));
  });

  it("says how to get pictures when there are none, and reports errors", async () => {
    render(<AppleScreens api={fakeApi([])} refreshKey={0} onError={vi.fn()} />);
    expect(await screen.findByText(/No screenshots yet|Noch keine Bildschirmfotos/)).toBeTruthy();
    cleanup();
    const onError = vi.fn();
    const broken = { screens: vi.fn(async () => Promise.reject(new Error("sandbox is down"))) } as unknown as ProjectApi;
    render(<AppleScreens api={broken} refreshKey={0} onError={onError} />);
    await waitFor(() => expect(onError).toHaveBeenCalledWith("sandbox is down"));
  });

  it("is a tab of Apple projects only", async () => {
    render(<ProjectPanel projectId="p1" canEdit refreshKey={0} onClose={vi.fn()} onError={vi.fn()} api={fakeApi()} />);
    expect(screen.queryByRole("tab", { name: /^(Devices|Geräte)$/ })).toBeNull();
    cleanup();
    render(
      <MemoryRouter>
        <ProjectPanel projectId="p1" canEdit apple refreshKey={0} onClose={vi.fn()} onError={vi.fn()} api={fakeApi()} />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("tab", { name: /^(Devices|Geräte)$/ }));
    expect(await screen.findByTestId("apple-screens")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Ready for approval|Zur Freigabe/ }).getAttribute("href")).toBe("/p/p1/apple");
  });
});
