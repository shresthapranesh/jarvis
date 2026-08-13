import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {SkillsQuery, SkillsQuery$data} from '../__generated__/SkillsQuery.graphql';
import type {Skill} from '../lib/types';
import {environment} from './environment';
import {decodeGlobalId} from './globalId';

export const skillsQuery = graphql`
  query SkillsQuery {
    skills {
      id
      name
      description
      body
      enabled
      createdAt
      updatedAt
    }
  }
`;

type Node = SkillsQuery$data['skills'][number];

export function mapSkill(s: Node): Skill {
  return {
    id: decodeGlobalId(s.id),
    name: s.name,
    description: s.description,
    body: s.body,
    enabled: s.enabled,
    created_at: s.createdAt,
    updated_at: s.updatedAt,
  };
}

export async function fetchSkills(): Promise<Skill[]> {
  const data = await fetchQuery<SkillsQuery>(
    environment,
    skillsQuery,
    {},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  return (data?.skills ?? []).map(mapSkill);
}

export function refreshSkills() {
  return fetchSkills().catch(() => undefined);
}
