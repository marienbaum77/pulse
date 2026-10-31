// @vitest-environment happy-dom
import "../../e2e/setup";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import {
  NumberInput,
  Badge,
  Toggle,
  Tabs,
  Field,
  Empty,
  Spinner,
  ErrorNote,
  Drawer,
  PageHeader,
} from "./ui";
import { Score, Breakdown, DailyBars, ArrivalStrip } from "./charts";
import { Thumb } from "./Thumb";

describe("Milestone 1 Stress Tests: Nothing UI & Shared Foundations", () => {
  describe("1. NumberInput Adversarial & Edge Cases", () => {
    it("handles zero gracefully", async () => {
      const onChange = vi.fn();
      render(<NumberInput value={0} onChange={onChange} min={0} max={100} />);
      const input = screen.getByRole("spinbutton") as HTMLInputElement;
      expect(input.value).toBe("0");

      fireEvent.change(input, { target: { value: "0" } });
      expect(input.value).toBe("0");
    });

    it("clamps values exceeding max on blur", async () => {
      const onChange = vi.fn();
      render(<NumberInput value={50} onChange={onChange} min={10} max={100} />);
      const input = screen.getByRole("spinbutton") as HTMLInputElement;

      fireEvent.change(input, { target: { value: "999" } });
      expect(onChange).not.toHaveBeenCalledWith(999); // outside range, doesn't immediately submit

      fireEvent.blur(input);
      expect(onChange).toHaveBeenCalledWith(100);
      expect(input.value).toBe("100");
    });

    it("clamps values below min on blur", async () => {
      const onChange = vi.fn();
      render(<NumberInput value={50} onChange={onChange} min={10} max={100} />);
      const input = screen.getByRole("spinbutton") as HTMLInputElement;

      fireEvent.change(input, { target: { value: "2" } });
      fireEvent.blur(input);
      expect(onChange).toHaveBeenCalledWith(10);
      expect(input.value).toBe("10");
    });

    it("reverts to fallback (or original value) on invalid NaN input or empty blur", async () => {
      const onChange = vi.fn();
      render(<NumberInput value={50} onChange={onChange} min={10} max={100} fallback={25} />);
      const input = screen.getByRole("spinbutton") as HTMLInputElement;

      fireEvent.change(input, { target: { value: "" } });
      fireEvent.blur(input);
      expect(onChange).toHaveBeenCalledWith(25);
      expect(input.value).toBe("25");
    });

    it("reverts to value if fallback is undefined and input is empty", async () => {
      const onChange = vi.fn();
      render(<NumberInput value={42} onChange={onChange} min={0} max={100} />);
      const input = screen.getByRole("spinbutton") as HTMLInputElement;

      fireEvent.change(input, { target: { value: "" } });
      fireEvent.blur(input);
      expect(onChange).toHaveBeenCalledWith(42);
      expect(input.value).toBe("42");
    });

    it("handles negative numbers when min allows negative", async () => {
      const onChange = vi.fn();
      render(<NumberInput value={-10} onChange={onChange} min={-50} max={50} />);
      const input = screen.getByRole("spinbutton") as HTMLInputElement;
      expect(input.value).toBe("-10");

      fireEvent.change(input, { target: { value: "-25" } });
      expect(onChange).toHaveBeenCalledWith(-25);

      fireEvent.blur(input);
      expect(input.value).toBe("-25");
    });

    it("supports decimal step with comma or dot replacement", async () => {
      const onChange = vi.fn();
      render(<NumberInput value={1.5} onChange={onChange} min={0} max={10} step={0.1} />);
      const input = screen.getByRole("spinbutton") as HTMLInputElement;

      fireEvent.change(input, { target: { value: "2.7" } });
      expect(onChange).toHaveBeenCalledWith(2.7);

      fireEvent.change(input, { target: { value: "3,4" } });
      expect(onChange).toHaveBeenCalledWith(3.4);
    });

    it("rounds to integer when step >= 1", async () => {
      const onChange = vi.fn();
      render(<NumberInput value={5} onChange={onChange} min={0} max={10} step={1} />);
      const input = screen.getByRole("spinbutton") as HTMLInputElement;

      fireEvent.change(input, { target: { value: "5.8" } });
      expect(onChange).toHaveBeenCalledWith(6);
    });

    it("respects disabled attribute", async () => {
      const onChange = vi.fn();
      render(<NumberInput value={5} onChange={onChange} min={0} max={10} disabled />);
      const input = screen.getByRole("spinbutton") as HTMLInputElement;
      expect(input).toBeDisabled();
    });

    it("preserves user text while typing (focused) even if value prop changes", async () => {
      const onChange = vi.fn();
      const { rerender } = render(<NumberInput value={5} onChange={onChange} min={0} max={100} />);
      const input = screen.getByRole("spinbutton") as HTMLInputElement;

      fireEvent.focus(input);
      fireEvent.change(input, { target: { value: "9" } });
      expect(input.value).toBe("9");

      // External prop updates to 10
      rerender(<NumberInput value={10} onChange={onChange} min={0} max={100} />);
      // User is still focused, so typing buffer '9' must NOT be blown away by 10
      expect(input.value).toBe("9");

      fireEvent.blur(input, { target: { value: input.value } });
      expect(onChange).toHaveBeenCalledWith(9);
      expect(input.value).toBe("9");
    });

    it("handles scientific notation and NaN inputs safely", async () => {
      const onChange = vi.fn();
      render(<NumberInput value={10} onChange={onChange} min={0} max={200} fallback={50} />);
      const input = screen.getByRole("spinbutton") as HTMLInputElement;

      fireEvent.change(input, { target: { value: "1e2" } });
      expect(onChange).toHaveBeenCalledWith(100);

      fireEvent.change(input, { target: { value: "not-a-number" } });
      // on invalid text, parse returns null, onChange should NOT be called with NaN
      expect(onChange).not.toHaveBeenCalledWith(NaN);

      fireEvent.blur(input);
      // fallback should be invoked
      expect(onChange).toHaveBeenCalledWith(50);
      expect(input.value).toBe("50");
    });

    it("handles min == max boundary condition gracefully", async () => {
      const onChange = vi.fn();
      render(<NumberInput value={5} onChange={onChange} min={5} max={5} />);
      const input = screen.getByRole("spinbutton") as HTMLInputElement;

      fireEvent.change(input, { target: { value: "10" } });
      fireEvent.blur(input);
      expect(onChange).toHaveBeenCalledWith(5);
      expect(input.value).toBe("5");
    });
  });

  describe("2. Badge Stress Testing", () => {
    const tones = ["ok", "warn", "bad", "info", "muted"] as const;

    tones.forEach((tone) => {
      it(`renders tone='${tone}' correctly without breaking`, () => {
        const { container } = render(<Badge tone={tone}>Status: {tone}</Badge>);
        const badge = container.querySelector("span");
        expect(badge).toBeInTheDocument();
        expect(badge?.textContent).toContain(`Status: ${tone}`);

        // LED dot inspection
        const dot = container.querySelector("span > span[aria-hidden]");
        expect(dot).toBeInTheDocument();
        const style = dot?.getAttribute("style") || "";
        expect(style).toContain(`var(--${tone})`);
        if (tone === "muted") {
          expect(style).toContain("box-shadow: none");
        } else {
          expect(style).toContain(`box-shadow: 0 0 5px var(--${tone})`);
        }
      });
    });

    it("handles empty children and long string children without layout collapse", () => {
      const { container: emptyContainer } = render(<Badge tone="info">{""}</Badge>);
      expect(emptyContainer.querySelector("span")).toBeInTheDocument();

      const longText = "VERY_LONG_TAG_THAT_COULD_POTENTIALLY_OVERFLOW_CONTAINER_WIDTH_AND_BREAK_FLEX";
      const { container: longContainer } = render(<Badge tone="ok">{longText}</Badge>);
      expect(longContainer.querySelector("span")?.textContent).toBe(longText);
    });
  });

  describe("3. Toggle Event Propagation & State Switch", () => {
    function ToggleHarness({ initial = false, disabled = false }: { initial?: boolean; disabled?: boolean }) {
      const [checked, setChecked] = useState(initial);
      return (
        <div data-testid="outer-container" onClick={() => {}}>
          <Toggle checked={checked} onChange={setChecked} label="Автопубликация" disabled={disabled} />
          <span data-testid="state">{checked ? "ON" : "OFF"}</span>
        </div>
      );
    }

    it("switches state when button is clicked", async () => {
      render(<ToggleHarness initial={false} />);
      const toggleBtn = screen.getByRole("switch");
      const state = screen.getByTestId("state");

      expect(toggleBtn).toHaveAttribute("aria-checked", "false");
      expect(state.textContent).toBe("OFF");

      await userEvent.click(toggleBtn);
      expect(toggleBtn).toHaveAttribute("aria-checked", "true");
      expect(state.textContent).toBe("ON");

      await userEvent.click(toggleBtn);
      expect(toggleBtn).toHaveAttribute("aria-checked", "false");
      expect(state.textContent).toBe("OFF");
    });

    it("switches state when label text is clicked", async () => {
      render(<ToggleHarness initial={false} />);
      const labelText = screen.getByText("Автопубликация");
      const toggleBtn = screen.getByRole("switch");
      const state = screen.getByTestId("state");

      await userEvent.click(labelText);
      expect(toggleBtn).toHaveAttribute("aria-checked", "true");
      expect(state.textContent).toBe("ON");
    });

    it("does NOT switch when disabled", async () => {
      render(<ToggleHarness initial={false} disabled={true} />);
      const toggleBtn = screen.getByRole("switch");
      const labelText = screen.getByText("Автопубликация");
      const state = screen.getByTestId("state");

      await userEvent.click(toggleBtn);
      expect(state.textContent).toBe("OFF");

      await userEvent.click(labelText);
      expect(state.textContent).toBe("OFF");
    });

    it("does not fire multiple duplicate onChange calls on a single user click", async () => {
      const onChange = vi.fn();
      render(<Toggle checked={false} onChange={onChange} label="Тестовый переключатель" />);
      const toggleBtn = screen.getByRole("switch");

      await userEvent.click(toggleBtn);
      expect(onChange).toHaveBeenCalledTimes(1);
      expect(onChange).toHaveBeenCalledWith(true);
    });

    it("does not trigger extra toggles when outer container has click listeners", async () => {
      const outerClick = vi.fn();
      const onChange = vi.fn();
      render(
        <div onClick={outerClick}>
          <Toggle checked={false} onChange={onChange} label="Вложенный переключатель" />
        </div>
      );
      const toggleBtn = screen.getByRole("switch");

      await userEvent.click(toggleBtn);
      expect(onChange).toHaveBeenCalledTimes(1);
      expect(onChange).toHaveBeenCalledWith(true);
    });
  });

  describe("4. Story Weight (NWS) Deprecated Stubs", () => {
    it("Score returns null and never renders DOM nodes", () => {
      render(
        <div data-testid="score-wrap">
          <Score value={85} width={100} />
          <Score value={null} />
          <Score />
        </div>
      );
      const wrap = screen.getByTestId("score-wrap");
      expect(wrap.children.length).toBe(0);
      expect(wrap.innerHTML).toBe("");
    });

    it("Breakdown returns null and never renders DOM nodes", () => {
      render(
        <div data-testid="breakdown-wrap">
          <Breakdown parts={{ test: { value: 1, weight: 2, contribution: 3 } }} />
          <Breakdown />
        </div>
      );
      const wrap = screen.getByTestId("breakdown-wrap");
      expect(wrap.children.length).toBe(0);
      expect(wrap.innerHTML).toBe("");
    });
  });

  describe("5. Charts Resilience & Edge Cases", () => {
    it("DailyBars handles empty data array without throwing or dividing by zero", () => {
      const { container } = render(<DailyBars data={[]} />);
      const svg = container.querySelector("svg");
      expect(svg).toBeInTheDocument();
    });

    it("DailyBars handles single day data and zero-item days", () => {
      const data = [
        { date: "2026-10-28", items: 0 },
        { date: "2026-10-29", items: 15 },
      ];
      const { container } = render(<DailyBars data={data} />);
      const rects = container.querySelectorAll("rect");
      expect(rects.length).toBe(2);
    });

    it("ArrivalStrip handles null arrivals safely", () => {
      const { container } = render(<ArrivalStrip arrivals={null} windowHours={24} />);
      expect(container.querySelector("svg")).toBeInTheDocument();
    });

    it("ArrivalStrip handles empty arrivals list", () => {
      const { container } = render(<ArrivalStrip arrivals={[]} windowHours={24} />);
      expect(container.querySelector("svg")).toBeInTheDocument();
    });

    it("ArrivalStrip renders dots for valid timestamps", () => {
      const now = new Date().toISOString();
      const earlier = new Date(Date.now() - 3600_000 * 2).toISOString();
      const { container } = render(<ArrivalStrip arrivals={[earlier, now]} windowHours={12} />);
      const circles = container.querySelectorAll("circle");
      // 1 marker circle + 2 data circles = 3 circles
      expect(circles.length).toBe(3);
    });
  });

  describe("6. Tabs, Thumb, ErrorNote, Spinner, Empty", () => {
    it("Tabs switches tab and displays count badges", async () => {
      const onTabChange = vi.fn();
      const tabs = [
        { id: "all", label: "Все", count: 12 },
        { id: "pending", label: "На рассмотрении", count: 3 },
      ];
      render(<Tabs tabs={tabs} value="all" onChange={onTabChange} />);

      const tabButtons = screen.getAllByRole("tab");
      expect(tabButtons.length).toBe(2);
      expect(tabButtons[0]).toHaveAttribute("aria-selected", "true");
      expect(tabButtons[1]).toHaveAttribute("aria-selected", "false");

      await userEvent.click(tabButtons[1]);
      expect(onTabChange).toHaveBeenCalledWith("pending");
    });

    it("Thumb handles error and hides smoothly", () => {
      const { container } = render(<Thumb src="/test.jpg" alt="test image" />);
      const img = container.querySelector("img");
      expect(img).toBeInTheDocument();

      fireEvent.error(img!);
      expect(container.querySelector("img")).not.toBeInTheDocument();
    });

    it("ErrorNote handles Error instance, string, and unknown objects", () => {
      const { rerender } = render(<ErrorNote error={new Error("Network failed")} />);
      expect(screen.getByRole("alert").textContent).toContain("Network failed");

      rerender(<ErrorNote error="Direct error message" />);
      expect(screen.getByRole("alert").textContent).toContain("Direct error message");

      rerender(<ErrorNote error={{ weird: 123 }} />);
      expect(screen.getByRole("alert").textContent).toContain("Что-то пошло не так");
    });

    it("Spinner renders with custom and default labels", () => {
      const { rerender } = render(<Spinner />);
      expect(screen.getByRole("status").textContent).toContain("Загрузка…");

      rerender(<Spinner label="Генерация" />);
      expect(screen.getByRole("status").textContent).toContain("Генерация…");
    });

    it("Empty state renders title and description", () => {
      render(<Empty title="Нет данных" text="Попробуйте обновить страницу" action={<button>Обновить</button>} />);
      expect(screen.getByText("Нет данных")).toBeInTheDocument();
      expect(screen.getByText("Попробуйте обновить страницу")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Обновить" })).toBeInTheDocument();
    });

    it("Drawer opens, renders children, and closes on Escape key", () => {
      const onClose = vi.fn();
      const { rerender } = render(
        <Drawer open={false} onClose={onClose} title="Свойства черновика">
          <div>Тело ящика</div>
        </Drawer>
      );
      expect(screen.queryByText("Свойства черновика")).not.toBeInTheDocument();

      rerender(
        <Drawer open={true} onClose={onClose} title="Свойства черновика">
          <div>Тело ящика</div>
        </Drawer>
      );
      expect(screen.getByText("Свойства черновика")).toBeInTheDocument();
      expect(screen.getByText("Тело ящика")).toBeInTheDocument();

      fireEvent.keyDown(window, { key: "Escape" });
      expect(onClose).toHaveBeenCalledTimes(1);
    });

    it("Field renders label and hint microtypography correctly", () => {
      render(
        <Field label="Имя источника" hint="Укажите уникальное имя">
          <input data-testid="field-input" />
        </Field>
      );
      expect(screen.getByText("Имя источника")).toBeInTheDocument();
      expect(screen.getByText("Укажите уникальное имя")).toBeInTheDocument();
      expect(screen.getByTestId("field-input")).toBeInTheDocument();
    });

    it("PageHeader renders title, subtitle, and action buttons cleanly", () => {
      render(
        <PageHeader
          title="Заголовок страницы"
          sub="Подзаголовок страницы"
          actions={<button>Действие</button>}
        />
      );
      expect(screen.getByRole("heading", { level: 1, name: "Заголовок страницы" })).toBeInTheDocument();
      expect(screen.getByText("Подзаголовок страницы")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Действие" })).toBeInTheDocument();
    });
  });
});
