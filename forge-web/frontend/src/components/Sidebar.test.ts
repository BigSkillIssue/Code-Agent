import { describe, expect, it } from "vitest";
import type { Project } from "../api/types";
import { searchSidebar } from "./Sidebar";

const project = (id: string, name: string): Project => ({ id, name, role: "owner", source: "empty", created_at: 0, updated_at: 0 });

describe("searchSidebar", () => {
  const projects = [project("p1", "Website"), project("p2", "Shop")];

  it("shows every project without a query", () => {
    expect(searchSidebar(projects, " ")).toEqual(projects);
  });

  it("finds projects by name, in any case", () => {
    expect(searchSidebar(projects, "SHOP")).toEqual([projects[1]]);
    expect(searchSidebar(projects, "nothing")).toEqual([]);
  });
});
