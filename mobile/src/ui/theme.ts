import { useColorScheme } from "react-native";

const dark = {
  bg: "#10151c",
  panel: "#151c25",
  raised: "#1b2430",
  line: "#26313e",
  text: "#e8edf4",
  secondary: "#b4c0cf",
  muted: "#8795a8",
  accent: "#a7efbc",
  onAccent: "#10151c",
  danger: "#e9bd81",
};
const light: typeof dark = {
  bg: "#f8fafb",
  panel: "#ffffff",
  raised: "#f0f4f6",
  line: "#dce2e8",
  text: "#1d2a37",
  secondary: "#4a5a6b",
  muted: "#657487",
  accent: "#25764c",
  onAccent: "#ffffff",
  danger: "#91601c",
};

export type Palette = typeof dark;

export function usePalette(): Palette {
  return useColorScheme() === "light" ? light : dark;
}

export const STATUS_LABELS: Record<string, string> = {
  pending: "Pending",
  in_progress: "In progress",
  blocked: "Blocked",
  review: "In review",
  done: "Done",
  cancelled: "Cancelled",
};
