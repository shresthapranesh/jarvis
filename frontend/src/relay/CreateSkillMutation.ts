import {commitMutation, graphql} from 'react-relay';

import type {CreateSkillMutation} from '../__generated__/CreateSkillMutation.graphql';
import type {Skill} from '../lib/types';
import {environment} from './environment';
import {mapSkill} from './SkillsQuery';

const mutation = graphql`
  mutation CreateSkillMutation($input: SkillCreateInput!) {
    createSkill(input: $input) {
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

export interface CreateSkillPayload {
  name: string;
  description: string;
  body: string;
  enabled?: boolean;
}

export function commitCreateSkill(p: CreateSkillPayload): Promise<Skill> {
  return new Promise((resolve, reject) => {
    commitMutation<CreateSkillMutation>(environment, {
      mutation,
      variables: {input: {...p, enabled: p.enabled ?? true}},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(mapSkill(response.createSkill));
      },
      onError: reject,
    });
  });
}
