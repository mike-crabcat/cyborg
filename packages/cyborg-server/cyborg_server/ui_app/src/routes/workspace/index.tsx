import { createFileRoute } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, useCallback } from "react";
import { fetchAPI, putAPI } from "@/lib/api";

interface Entry {
  name: string;
  type: "file" | "dir";
  size_bytes?: number;
}

interface ListResult {
  entries: Entry[];
  path: string;
  root: string;
}

interface FileResult {
  type: "text" | "binary";
  content?: string;
  size_bytes?: number;
  path: string;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

function fileIcon(name: string): string {
  if (name.endsWith(".py")) return "py";
  if (name.endsWith(".md")) return "md";
  if (name.endsWith(".json")) return "{}";
  if (name.endsWith(".ts") || name.endsWith(".tsx")) return "ts";
  if (name.endsWith(".js")) return "js";
  if (name.endsWith(".sql")) return "db";
  if (/\.(png|jpg|jpeg|gif|webp|svg|bmp)$/i.test(name)) return "img";
  if (name.endsWith(".pdf")) return "pdf";
  if (name.endsWith(".txt")) return "txt";
  return "~";
}

function isImageFile(name: string): boolean {
  return /\.(png|jpg|jpeg|gif|webp|svg|bmp|ico)$/i.test(name);
}

function isPdfFile(name: string): boolean {
  return /\.pdf$/i.test(name);
}

function WorkspacePage() {
  const queryClient = useQueryClient();
  const [currentPath, setCurrentPath] = useState("");
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState("");

  const { data: listing } = useQuery<ListResult>({
    queryKey: ["workspace", currentPath],
    queryFn: () => fetchAPI<ListResult>(`/workspace?path=${encodeURIComponent(currentPath)}&depth=1`),
  });

  const { data: fileData, isLoading: fileLoading } = useQuery<FileResult>({
    queryKey: ["workspace-file", selectedFile],
    queryFn: () => fetchAPI<FileResult>(`/workspace/file?path=${encodeURIComponent(selectedFile!)}`),
    enabled: selectedFile !== null && !isImageFile(selectedFile) && !isPdfFile(selectedFile),
  });

  const saveMutation = useMutation({
    mutationFn: (content: string) =>
      putAPI<{ ok: boolean }>(`/workspace/file?path=${encodeURIComponent(selectedFile!)}`, { content }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workspace-file", selectedFile] });
      setEditing(false);
    },
  });

  const startEdit = useCallback(() => {
    if (fileData?.content !== undefined) {
      setEditContent(fileData.content);
      setEditing(true);
    }
  }, [fileData]);

  const copyContent = useCallback(async () => {
    const text = fileData?.content ?? "";
    await navigator.clipboard.writeText(text);
  }, [fileData]);

  const copyPath = useCallback(async () => {
    if (selectedFile) await navigator.clipboard.writeText(selectedFile);
  }, [selectedFile]);

  const navigateTo = (name: string) => {
    const newPath = currentPath ? `${currentPath}/${name}` : name;
    setCurrentPath(newPath);
    setSelectedFile(null);
    setEditing(false);
  };

  const goUp = () => {
    const parts = currentPath.split("/");
    parts.pop();
    setCurrentPath(parts.join("/"));
    setSelectedFile(null);
    setEditing(false);
  };

  const entries = listing?.entries ?? [];
  const dirs = entries.filter((e) => e.type === "dir");
  const files = entries.filter((e) => e.type === "file");
  const sorted = [...dirs, ...files];

  const selectedIsImage = selectedFile ? isImageFile(selectedFile) : false;
  const selectedIsPdf = selectedFile ? isPdfFile(selectedFile) : false;

  return (
    <div className="flex flex-col h-full">
      {/* Breadcrumb */}
      <div className="flex items-center gap-1 px-3 py-2 border-b border-border text-xs shrink-0 overflow-x-auto">
        <button onClick={() => { setCurrentPath(""); setSelectedFile(null); }} className="text-accent hover:underline shrink-0">
          root
        </button>
        {currentPath.split("/").filter(Boolean).map((part, i, arr) => {
          const partial = arr.slice(0, i + 1).join("/");
          return (
            <span key={partial} className="flex items-center gap-1 shrink-0">
              <span className="text-muted">/</span>
              <button onClick={() => { setCurrentPath(partial); setSelectedFile(null); }} className="text-accent hover:underline">
                {part}
              </button>
            </span>
          );
        })}
      </div>

      <div className="flex-1 min-h-0 flex flex-col md:flex-row">
        {/* File list */}
        <div className="md:w-56 shrink-0 md:border-r border-b md:border-b-0 border-border overflow-y-auto max-h-36 md:max-h-none flex flex-col">
          {currentPath && (
            <button onClick={goUp} className="text-left px-2 py-1.5 text-xs text-muted hover:bg-surface border-b border-border">
              ..
            </button>
          )}
          {sorted.map((entry) => {
            const displayName = entry.name.split("/").pop() ?? entry.name;
            const isSelected = selectedFile === entry.name;
            return (
              <button
                key={entry.name}
                onClick={() => entry.type === "dir" ? navigateTo(displayName) : (setSelectedFile(entry.name), setEditing(false))}
                className={`text-left px-2 py-1.5 text-xs border-b border-border hover:bg-surface transition-colors flex items-center gap-1.5 ${
                  isSelected ? "bg-surface text-accent" : "text-text"
                }`}
              >
                <span className="text-muted w-5 text-center shrink-0 text-[10px]">
                  {entry.type === "dir" ? "D" : fileIcon(displayName)}
                </span>
                <span className="truncate flex-1">{displayName}</span>
                {entry.size_bytes !== undefined && (
                  <span className="text-muted text-[10px] shrink-0">{formatSize(entry.size_bytes)}</span>
                )}
              </button>
            );
          })}
          {sorted.length === 0 && !fileLoading && (
            <div className="px-3 py-4 text-xs text-muted text-center">empty directory</div>
          )}
        </div>

        {/* File viewer */}
        <div className="flex-1 min-w-0 overflow-hidden flex flex-col">
          {selectedFile === null ? (
            <div className="flex-1 flex items-center justify-center text-xs text-muted">
              select a file to view
            </div>
          ) : selectedIsImage ? (
            <div className="flex-1 overflow-auto p-3 flex flex-col gap-2">
              <div className="flex items-center gap-2 shrink-0">
                <span className="text-xs text-muted">{selectedFile.split("/").pop()}</span>
                <button onClick={copyPath} className="text-[10px] text-accent hover:underline">copy path</button>
              </div>
              <img
                src={`/dashboard/api/workspace/file?path=${encodeURIComponent(selectedFile)}`}
                alt={selectedFile}
                className="max-w-full object-contain"
              />
            </div>
          ) : selectedIsPdf ? (
            <div className="flex-1 min-h-0 flex flex-col">
              <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border shrink-0">
                <span className="text-xs text-muted">{selectedFile.split("/").pop()}</span>
                <button onClick={copyPath} className="text-[10px] text-accent hover:underline">copy path</button>
              </div>
              <iframe
                src={`/dashboard/api/workspace/file?path=${encodeURIComponent(selectedFile)}`}
                className="flex-1 w-full min-h-0 border-0"
                title={selectedFile}
              />
            </div>
          ) : fileLoading ? (
            <div className="flex-1 flex items-center justify-center text-xs text-muted">loading...</div>
          ) : fileData?.type === "binary" ? (
            <div className="flex-1 flex items-center justify-center text-xs text-muted">
              binary file ({formatSize(fileData.size_bytes ?? 0)})
            </div>
          ) : editing ? (
            <div className="flex flex-col flex-1 min-h-0">
              <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border shrink-0">
                <span className="text-xs text-text">{selectedFile.split("/").pop()}</span>
                <div className="flex-1" />
                <button onClick={() => saveMutation.mutate(editContent)} disabled={saveMutation.isPending} className="text-[10px] bg-accent text-bg px-2 py-0.5 hover:opacity-90 disabled:opacity-50">
                  {saveMutation.isPending ? "saving..." : "save"}
                </button>
                <button onClick={() => setEditing(false)} disabled={saveMutation.isPending} className="text-[10px] text-muted hover:text-text px-2 py-0.5">
                  cancel
                </button>
                {saveMutation.isError && <span className="text-[10px] text-red-400">save failed</span>}
              </div>
              <textarea
                value={editContent}
                onChange={(e) => setEditContent(e.target.value)}
                className="flex-1 bg-bg text-text text-xs font-mono p-3 resize-none outline-none min-h-0"
                spellCheck={false}
              />
            </div>
          ) : (
            <div className="flex flex-col flex-1 min-h-0">
              <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border shrink-0">
                <span className="text-xs text-text">{selectedFile.split("/").pop()}</span>
                {fileData?.size_bytes !== undefined && <span className="text-[10px] text-muted">{formatSize(fileData.size_bytes)}</span>}
                <div className="flex-1" />
                <button onClick={startEdit} className="text-[10px] text-accent hover:underline">edit</button>
                <button onClick={copyContent} className="text-[10px] text-accent hover:underline">copy</button>
                <button onClick={copyPath} className="text-[10px] text-accent hover:underline">copy path</button>
              </div>
              <pre className="flex-1 overflow-auto p-3 text-xs font-mono text-text whitespace-pre min-h-0">
                {fileData?.content ?? ""}
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export const Route = createFileRoute("/workspace/")({ component: WorkspacePage });
