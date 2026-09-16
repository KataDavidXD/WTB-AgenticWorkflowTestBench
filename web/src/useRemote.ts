import { useEffect, useState } from 'react';
import { api } from './service';
export function useRemote<T>(path: string | undefined, revision = 0) {
  const [result, setResult] = useState<{ path?: string; data?: T; error?: string }>({});
  useEffect(() => {
    let active = true;
    if (path) void api<T>(path).then(data => { if (active) setResult({ path, data }); }).catch(error => { if (active) setResult({ path, error: String(error) }); });
    return () => { active = false; };
  }, [path, revision]);
  return result.path === path ? result : {};
}
