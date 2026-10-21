import { useCallback } from 'react';
import { api } from '@/services/apiClient';
import { useAsync } from '@/hooks/useAsync';
import type { FacebookAccount, GroupRow, JobRow, Pipeline } from '@/types/api';

/** The loading and error state of one workspace resource. */
export interface ResourceState {
  loading: boolean;
  error: string | null;
}

export interface WorkspaceData {
  /** The numeric id of the workspace, known as soon as the route is parsed. */
  pipelineId: number | null;
  pipeline: Pipeline | null;
  /** Every workspace the signed in user may open. */
  pipelines: Pipeline[];
  groups: GroupRow[];
  accounts: FacebookAccount[];
  jobs: JobRow[];
  /**
   * The state of each request. A failed request is an error, never an empty
   * list: the interface must be able to tell the two apart.
   */
  groupsState: ResourceState;
  accountsState: ResourceState;
  jobsState: ResourceState;
  reloadAll: () => void;
  reloadGroups: () => void;
  reloadAccounts: () => void;
}

/** Everything a workspace page needs, always scoped to one pipeline. */
export function useWorkspace(pipelineId: number | null): WorkspaceData {
  const id = pipelineId ?? 0;

  const pipelines = useAsync<Pipeline[]>(
    (signal) => api.get('/api/pipelines', signal),
    [],
  );

  const groups = useAsync<GroupRow[]>(
    (signal) => (id ? api.get(`/api/pipelines/${id}/groups`, signal) : Promise.resolve([])),
    [id],
  );

  const accounts = useAsync<FacebookAccount[]>(
    (signal) => (id ? api.get(`/api/accounts?pipeline_id=${id}`, signal) : Promise.resolve([])),
    [id],
  );

  const jobs = useAsync<JobRow[]>(
    (signal) => (id ? api.get(`/api/jobs?pipeline_id=${id}`, signal) : Promise.resolve([])),
    [id],
  );

  const reloadAll = useCallback(() => {
    pipelines.reload();
    groups.reload();
    accounts.reload();
    jobs.reload();
  }, [pipelines.reload, groups.reload, accounts.reload, jobs.reload]);

  const reloadGroups = useCallback(() => groups.reload(), [groups.reload]);
  const reloadAccounts = useCallback(() => accounts.reload(), [accounts.reload]);

  const pipeline = (pipelines.data ?? []).find((row) => row.id === id) ?? null;

  return {
    pipelineId: id || null,
    pipeline,
    pipelines: pipelines.data ?? [],
    groups: groups.data ?? [],
    accounts: accounts.data ?? [],
    jobs: jobs.data ?? [],
    groupsState: { loading: groups.loading, error: groups.error },
    accountsState: { loading: accounts.loading, error: accounts.error },
    jobsState: { loading: jobs.loading, error: jobs.error },
    reloadAll,
    reloadGroups,
    reloadAccounts,
  };
}
