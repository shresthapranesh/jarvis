import {commitMutation, graphql} from 'react-relay';

import type {UpdateSkillMutation} from '../__generated__/UpdateSkillMutation.graphql';
import type {Skill} from '../lib/types';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';
import {mapSkill} from './SkillsQuery';

const mutation = graphql`
  mutation UpdateSkillMutation($id: ID!, $input: SkillUpdateInput!) {
    updateSkill(id: $id, input: $input) {
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

export interface UpdateSkillPayload {
  name?: string;
  description?: string;
  body?: string;
  enabled?: boolean;
}

export function commitUpdateSkill(rawId: string, p: UpdateSkillPayload): Promise<Skill> {
  return new Promise((resolve, reject) => {
    commitMutation<UpdateSkillMutation>(environment, {
      mutation,
      variables: {
        id: encodeGlobalId('Skill', rawId),
        input: {
          name: p.name ?? null,
          description: p.description ?? null,
          body: p.body ?? null,
          enabled: p.enabled ?? null,
        },
      },
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(mapSkill(response.updateSkill));
      },
      onError: reject,
    });
  });
}
