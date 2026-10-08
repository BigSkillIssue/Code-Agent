import { describe, expect, it } from "vitest";
import type { Chat, Project } from "../api/types";
import { searchSidebar } from "./Sidebar";

const project = (id: string, name: string): Project => ({ id, name, role: "owner", source: "empty", created_at: 0, updated_at: 0 });
const chat = (id: string, projectId: string, title: string): Chat => ({
  id, project_id: projectId, title, state: "idle", mode: "edits", model: "", shared: false, mine: true, created_at: 0, updated_at: 0,
});

describe("searchSidebar", () => {
  const projects = [project("p1", "Website"), project("p2", "Shop")];
  const chats = { p1: [chat("c1", "p1", "Fix the header"), chat("c2", "p1", "Add a footer")], p2: [chat("c3", "p2", "Cart totals")] };

  it("shows everything without a query", () => {
    expect(searchSidebar(projects, chats, " ").map((r) => r.chats?.length)).toEqual([2, 1]);
  });

  it("finds projects by name and chats by title", () => {
    expect(searchSidebar(projects, chats, "shop")).toEqual([{ project: projects[1], chats: chats.p2 }]);
    expect(searchSidebar(projects, chats, "FOOT")).toEqual([{ project: projects[0], chats: [chats.p1[1]] }]);
    expect(searchSidebar(projects, chats, "nothing")).toEqual([]);
  });
});
