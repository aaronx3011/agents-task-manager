// A small Markdown renderer for task descriptions, results and chat messages:
// headings, paragraphs, lists (incl. task lists), quotes, fenced code, rules,
// and inline bold/italic/code/links. Unsupported syntax renders as plain text.
import { Fragment } from "react";
import type { ReactNode } from "react";
import { Linking, Platform, ScrollView, Text, View } from "react-native";
import { usePalette } from "@/ui/theme";
import type { Palette } from "@/ui/theme";

type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "paragraph"; text: string }
  | { kind: "list"; ordered: boolean; items: { text: string; checked: boolean | null }[] }
  | { kind: "quote"; text: string }
  | { kind: "code"; text: string }
  | { kind: "rule" };

const MONO = Platform.OS === "ios" ? "Menlo" : "monospace";

export function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (/^\s*```/.test(line)) {
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !/^\s*```/.test(lines[index])) code.push(lines[index++]);
      index += 1;
      blocks.push({ kind: "code", text: code.join("\n") });
      continue;
    }
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push({ kind: "heading", level: heading[1].length, text: heading[2] });
      index += 1;
      continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      blocks.push({ kind: "rule" });
      index += 1;
      continue;
    }
    if (/^\s*>/.test(line)) {
      const quote: string[] = [];
      while (index < lines.length && /^\s*>/.test(lines[index]))
        quote.push(lines[index++].replace(/^\s*>\s?/, ""));
      blocks.push({ kind: "quote", text: quote.join(" ") });
      continue;
    }
    const item = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(line);
    if (item) {
      const ordered = /\d/.test(item[1]);
      const items: { text: string; checked: boolean | null }[] = [];
      while (index < lines.length) {
        const match = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(lines[index]);
        if (!match) {
          // Indented continuation lines belong to the previous item.
          if (lines[index].trim() && /^\s{2,}/.test(lines[index]) && items.length) {
            items[items.length - 1].text += ` ${lines[index].trim()}`;
            index += 1;
            continue;
          }
          break;
        }
        const task = /^\[([ xX])\]\s+(.*)$/.exec(match[2]);
        items.push(
          task ? { text: task[2], checked: task[1] !== " " } : { text: match[2], checked: null },
        );
        index += 1;
      }
      blocks.push({ kind: "list", ordered, items });
      continue;
    }
    const paragraph: string[] = [];
    while (
      index < lines.length &&
      lines[index].trim() &&
      !/^(#{1,6}\s|\s*```|\s*>|\s*([-*+]|\d+[.)])\s)/.test(lines[index])
    )
      paragraph.push(lines[index++].trim());
    blocks.push({ kind: "paragraph", text: paragraph.join(" ") });
  }
  return blocks;
}

function inline(text: string, palette: Palette, key = ""): ReactNode[] {
  const pattern =
    /(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|\*[^*\s][^*]*\*|_[^_\s][^_]*_|\[[^\]]+\]\([^)\s]+\)|https?:\/\/[^\s)]+)/g;
  const parts: ReactNode[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  let n = 0;
  while ((match = pattern.exec(text))) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    const token = match[0];
    const id = `${key}-${n++}`;
    if (token.startsWith("`"))
      parts.push(
        <Text key={id} style={{ fontFamily: MONO, backgroundColor: palette.raised }}>
          {token.slice(1, -1)}
        </Text>,
      );
    else if (token.startsWith("**") || token.startsWith("__"))
      parts.push(
        <Text key={id} style={{ fontWeight: "700" }}>
          {inline(token.slice(2, -2), palette, id)}
        </Text>,
      );
    else if (token.startsWith("[")) {
      const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(token)!;
      parts.push(<LinkText key={id} url={link[2]} label={link[1]} palette={palette} />);
    } else if (token.startsWith("http"))
      parts.push(<LinkText key={id} url={token} label={token} palette={palette} />);
    else
      parts.push(
        <Text key={id} style={{ fontStyle: "italic" }}>
          {inline(token.slice(1, -1), palette, id)}
        </Text>,
      );
    last = match.index + token.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

function LinkText({ url, label, palette }: { url: string; label: string; palette: Palette }) {
  const safe = /^https?:\/\//.test(url);
  return (
    <Text
      accessibilityRole={safe ? "link" : undefined}
      style={{ color: palette.accent, textDecorationLine: safe ? "underline" : "none" }}
      onPress={safe ? () => void Linking.openURL(url) : undefined}
    >
      {label}
    </Text>
  );
}

export function Markdown({ text, size = 15 }: { text: string; size?: number }) {
  const palette = usePalette();
  const body = { color: palette.text, fontSize: size, lineHeight: size * 1.45 };
  return (
    <View style={{ gap: 8 }}>
      {parseBlocks(text).map((block, i) => {
        const key = String(i);
        switch (block.kind) {
          case "heading":
            return (
              <Text
                key={key}
                accessibilityRole="header"
                style={{
                  ...body,
                  fontWeight: "700",
                  fontSize: size + Math.max(0, 6 - block.level * 1.5),
                }}
              >
                {inline(block.text, palette, key)}
              </Text>
            );
          case "paragraph":
            return (
              <Text key={key} selectable style={body}>
                {inline(block.text, palette, key)}
              </Text>
            );
          case "quote":
            return (
              <View
                key={key}
                style={{ borderLeftWidth: 3, borderColor: palette.line, paddingLeft: 10 }}
              >
                <Text style={{ ...body, color: palette.secondary }}>
                  {inline(block.text, palette, key)}
                </Text>
              </View>
            );
          case "code":
            return (
              <ScrollView
                key={key}
                horizontal
                style={{ backgroundColor: palette.raised, borderRadius: 6 }}
                contentContainerStyle={{ padding: 10 }}
              >
                <Text
                  selectable
                  style={{ fontFamily: MONO, fontSize: size - 2, color: palette.text }}
                >
                  {block.text}
                </Text>
              </ScrollView>
            );
          case "rule":
            return <View key={key} style={{ height: 1, backgroundColor: palette.line }} />;
          case "list":
            return (
              <View key={key} style={{ gap: 4 }}>
                {block.items.map((item, n) => (
                  <View key={n} style={{ flexDirection: "row", gap: 8 }}>
                    <Text style={{ ...body, color: palette.muted, minWidth: 18 }}>
                      {item.checked === true
                        ? "☑"
                        : item.checked === false
                          ? "☐"
                          : block.ordered
                            ? `${n + 1}.`
                            : "•"}
                    </Text>
                    <Text selectable style={{ ...body, flex: 1 }}>
                      {inline(item.text, palette, `${key}-${n}`).map((part, p) => (
                        <Fragment key={p}>{part}</Fragment>
                      ))}
                    </Text>
                  </View>
                ))}
              </View>
            );
        }
      })}
    </View>
  );
}
