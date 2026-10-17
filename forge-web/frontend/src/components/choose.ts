// Tests pick an option of Forge's own Select (W43) as a person does: open it, click the option.

import { fireEvent, screen } from "@testing-library/react";

/** Open the Select and click the option with this name. */
export async function choose(field: HTMLElement, option: string | RegExp): Promise<void> {
  fireEvent.click(field);
  fireEvent.click(await screen.findByRole("option", { name: option }));
}
