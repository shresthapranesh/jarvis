import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {ProjectsQuery, ProjectsQuery$data} from '../__generated__/ProjectsQuery.graphql';
import type {Project} from '../lib/types';
import {environment} from './environment';
import {decodeGlobalId} from './globalId';

export const projectsQuery = graphql`
  query ProjectsQuery {
    projects {
      id
      name
      description
      conversationCount
      createdAt
      updatedAt
    }
  }
`;

type Node = ProjectsQuery$data['projects'][number];

export function mapProject(p: Node): Project {
  return {
    id: decodeGlobalId(p.id),
    name: p.name,
    description: p.description ?? null,
    conversation_count: p.conversationCount,
    created_at: p.createdAt,
    updated_at: p.updatedAt,
  };
}

export async function fetchProjects(): Promise<Project[]> {
  const data = await fetchQuery<ProjectsQuery>(
    environment,
    projectsQuery,
    {},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  return (data?.projects ?? []).map(mapProject);
}
