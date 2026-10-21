import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '@/services/apiClient';

interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

/** Runs a request and keeps the loading and error state with it. */
export function useAsync<T>(loader: (signal: AbortSignal) => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    loader(controller.signal)
      .then((result) => {
        if (!mounted.current || controller.signal.aborted) return;
        setData(result);
        setLoading(false);
      })
      .catch((failure: unknown) => {
        if (!mounted.current || controller.signal.aborted) return;
        if (failure instanceof ApiError && failure.isAuthError) {
          setError('Phiên đăng nhập đã hết hạn.');
        } else {
          setError(failure instanceof Error ? failure.message : 'Không tải được dữ liệu');
        }
        setLoading(false);
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((value) => value + 1), []);
  return { data, loading, error, reload };
}
