export type SearchHistoryItem = {
  id: string;
  query: string;
  url?: string;
  createdAt: string;
};

export type SavedAnalysisItem = {
  id: string;
  title: string;
  query: string;
  url?: string;
  summary?: string;
  tags: string[];
  memo?: string;
  favorite: boolean;
  createdAt: string;
};

const HISTORY_KEY = "trustlen_recent_search_history";
const ARCHIVE_KEY = "trustlen_saved_analysis_archive";

export function getSearchHistory(): SearchHistoryItem[] {
  return JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
}

export function addSearchHistory(item: Omit<SearchHistoryItem, "id" | "createdAt">) {
  const next = [
    { ...item, id: crypto.randomUUID(), createdAt: new Date().toISOString() },
    ...getSearchHistory(),
  ].slice(0, 30);

  localStorage.setItem(HISTORY_KEY, JSON.stringify(next));
  return next;
}

export function getSavedAnalyses(): SavedAnalysisItem[] {
  return JSON.parse(localStorage.getItem(ARCHIVE_KEY) || "[]");
}

export function saveAnalysis(item: Omit<SavedAnalysisItem, "id" | "createdAt" | "favorite">) {
  const next = [
    {
      ...item,
      id: crypto.randomUUID(),
      favorite: false,
      createdAt: new Date().toISOString(),
    },
    ...getSavedAnalyses(),
  ];

  localStorage.setItem(ARCHIVE_KEY, JSON.stringify(next));
  return next;
}

export function updateSavedAnalysis(id: string, patch: Partial<SavedAnalysisItem>) {
  const next = getSavedAnalyses().map((item) =>
    item.id === id ? { ...item, ...patch } : item
  );

  localStorage.setItem(ARCHIVE_KEY, JSON.stringify(next));
  return next;
}

export function deleteSavedAnalysis(id: string) {
  const next = getSavedAnalyses().filter((item) => item.id !== id);
  localStorage.setItem(ARCHIVE_KEY, JSON.stringify(next));
  return next;
}
