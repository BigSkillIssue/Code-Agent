import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import favicon from "../../public/favicon.svg?raw";
import { ANVIL_PATH, Logo, LogoMark, SPARK_PATH } from "./Logo";

afterEach(cleanup);

describe("Logo", () => {
  it("shows the anvil, its ember spark and the word mark", () => {
    const { container } = render(<Logo size={30} />);
    expect(screen.getByText("Forge")).toBeTruthy();
    const svg = container.querySelector("svg")!;
    expect(svg.getAttribute("aria-hidden")).toBe("true"); // the word mark names it
    expect(svg.getAttribute("width")).toBe("30");
    expect(svg.querySelector(`path[d="${ANVIL_PATH}"]`)?.getAttribute("fill")).toBe("currentColor");
    expect(svg.querySelectorAll(".logo-spark")).toHaveLength(2);
  });

  it("is an image named Forge without the word mark", () => {
    render(<Logo wordmark={false} />);
    expect(screen.getByRole("img", { name: "Forge" })).toBeTruthy();
    expect(screen.queryByText("Forge")).toBeNull();
  });

  it("is decoration as a bare mark", () => {
    const { container } = render(<LogoMark size={56} />);
    expect(container.querySelector("svg")?.getAttribute("aria-hidden")).toBe("true");
  });

  it("draws the same anvil and spark as the favicon", () => {
    expect(favicon).toContain(`d="${ANVIL_PATH}"`);
    expect(favicon).toContain(`d="${SPARK_PATH}"`);
  });
});
