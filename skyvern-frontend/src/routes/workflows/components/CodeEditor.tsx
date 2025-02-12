import CodeMirror from "@uiw/react-codemirror";
import { json } from "@codemirror/lang-json";
import { python } from "@codemirror/lang-python";
import { tokyoNightStorm } from "@uiw/codemirror-theme-tokyo-night-storm";
import { cn } from "@/util/utils";

type Props = {
  value: string;
  onChange: (value: string) => void;
  language: "python" | "json";
  disabled?: boolean;
  minHeight?: string;
  className?: string;
  fontSize?: number;
};

function CodeEditor({
  value,
  onChange,
  minHeight,
  language,
  className,
  fontSize = 8,
}: Props) {
  const extensions = language === "json" ? [json()] : [python()];
  return (
    <CodeMirror
      value={value}
      onChange={onChange}
      extensions={extensions}
      theme={tokyoNightStorm}
      minHeight={minHeight}
      className={cn("cursor-auto", className)}
      style={{
        fontSize: fontSize,
      }}
    />
  );
}

export { CodeEditor };
