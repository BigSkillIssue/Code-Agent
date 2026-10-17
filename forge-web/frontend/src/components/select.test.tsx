import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Segmented } from "./Segmented";
import { type Choice, Select } from "./Select";

const ROLES: Choice[] = [
  { value: "member", label: "Member", description: "Uses Forge" },
  { value: "admin", label: "Admin", description: "Runs the server" },
  { value: "auditor", label: "Auditor" },
];

function Harness({ onChange }: { onChange: (v: string) => void }) {
  const [value, setValue] = useState("member");
  return (
    <>
      <Select label="Role" value={value} options={ROLES} onChange={(v) => { setValue(v); onChange(v); }} />
      <button type="button">elsewhere</button>
    </>
  );
}

afterEach(cleanup);

describe("Select", () => {
  it("opens on click, shows descriptions, ticks the chosen one and picks with a click", () => {
    const changed = vi.fn();
    render(<Harness onChange={changed} />);
    const field = screen.getByRole("combobox", { name: "Role" });
    expect(field.textContent).toContain("Member");
    expect(field.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(field);
    expect(field.getAttribute("aria-expanded")).toBe("true");
    const options = screen.getAllByRole("option");
    expect(options.map((o) => o.getAttribute("aria-selected"))).toEqual(["true", "false", "false"]);
    expect(screen.getByText("Runs the server")).toBeTruthy();
    fireEvent.click(screen.getByRole("option", { name: /Admin/ }));
    expect(changed).toHaveBeenCalledWith("admin");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(field.textContent).toContain("Admin");
  });

  it("works with the keyboard: arrows, Enter, Escape and typing a letter", () => {
    const changed = vi.fn();
    render(<Harness onChange={changed} />);
    const field = screen.getByRole("combobox", { name: "Role" });
    fireEvent.keyDown(field, { key: "ArrowDown" }); // opens on the chosen option
    expect(screen.getByRole("listbox")).toBeTruthy();
    fireEvent.keyDown(field, { key: "ArrowDown" });
    expect(field.getAttribute("aria-activedescendant")).toBe(screen.getAllByRole("option")[1].id);
    fireEvent.keyDown(field, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(changed).not.toHaveBeenCalled();
    fireEvent.keyDown(field, { key: "Enter" });
    fireEvent.keyDown(field, { key: "a" });
    fireEvent.keyDown(field, { key: "u" }); // "au": Auditor, not Admin
    fireEvent.keyDown(field, { key: "Enter" });
    expect(changed).toHaveBeenLastCalledWith("auditor");
  });

  it("closes on a click elsewhere and does nothing while disabled", () => {
    const { rerender } = render(<Select ariaLabel="Plan" value="a" options={[{ value: "a", label: "A" }]} onChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("combobox", { name: "Plan" }));
    expect(screen.getByRole("listbox")).toBeTruthy();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("listbox")).toBeNull();
    rerender(<Select ariaLabel="Plan" value="a" disabled options={[{ value: "a", label: "A" }]} onChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("combobox", { name: "Plan" }));
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});

describe("Segmented", () => {
  it("is a radio group the arrow keys move through", () => {
    const changed = vi.fn();
    render(<Segmented ariaLabel="Billing" value="month" onChange={changed}
      options={[{ value: "month", label: "Monthly" }, { value: "year", label: "Yearly" }]} />);
    const [month, year] = screen.getAllByRole("radio");
    expect(month.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(year);
    expect(changed).toHaveBeenLastCalledWith("year");
    fireEvent.keyDown(month, { key: "ArrowRight" });
    expect(changed).toHaveBeenLastCalledWith("year");
    fireEvent.keyDown(month, { key: "ArrowLeft" });
    expect(changed).toHaveBeenLastCalledWith("year"); // wraps around
  });
});
