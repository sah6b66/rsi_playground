// Monaco DiffEditor 封装（DD-14 §7）：只读、双栏。
import { DiffEditor } from '@monaco-editor/react';

export interface DiffViewerProps {
  oldText: string;
  newText: string;
  language?: string;
  height?: number;
}

export function DiffViewer({
  oldText,
  newText,
  language = 'markdown',
  height = 480,
}: DiffViewerProps) {
  return (
    <div data-testid="monaco-diff" style={{ height }}>
      <DiffEditor
        height={height}
        language={language}
        original={oldText}
        modified={newText}
        options={{
          readOnly: true,
          renderSideBySide: true,
          renderOverviewRuler: false,
          minimap: { enabled: false },
          scrollBeyondLastLine: false,
        }}
      />
    </div>
  );
}
