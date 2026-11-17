/** Push-to-talk gesture ownership, kept separate so it can be regression tested. */
export type HoldOutcome = "none" | "send" | "cancel";

export function pointerWithinRect(
  rect: { left: number; top: number; right: number; bottom: number },
  x: number,
  y: number,
): boolean {
  return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
}

export class HoldToTalk {
  private pointerId?: number;
  private keyboard = false;

  get active(): boolean {
    return this.pointerId !== undefined || this.keyboard;
  }

  beginPointer(id: number): boolean {
    if (this.active) return false;
    this.pointerId = id;
    return true;
  }

  beginKeyboard(): boolean {
    if (this.active) return false;
    this.keyboard = true;
    return true;
  }

  /** Only the pointer that started the gesture can finish it. */
  endPointer(id: number, inside: boolean): HoldOutcome {
    if (this.pointerId !== id) return "none";
    this.pointerId = undefined;
    return inside ? "send" : "cancel";
  }

  endKeyboard(): HoldOutcome {
    if (!this.keyboard) return "none";
    this.keyboard = false;
    return "send";
  }

  cancel(): HoldOutcome {
    if (!this.active) return "none";
    this.pointerId = undefined;
    this.keyboard = false;
    return "cancel";
  }
}
