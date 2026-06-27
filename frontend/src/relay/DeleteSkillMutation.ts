import {commitMutation, graphql} from 'react-relay';

import type {DeleteSkillMutation} from '../__generated__/DeleteSkillMutation.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

const mutation = graphql`
  mutation DeleteSkillMutation($id: ID!) {
    deleteSkill(id: $id)
  }
`;

export function commitDeleteSkill(rawId: string): Promise<boolean> {
  return new Promise((resolve, reject) => {
    commitMutation<DeleteSkillMutation>(environment, {
      mutation,
      variables: {id: encodeGlobalId('Skill', rawId)},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.deleteSkill);
      },
      onError: reject,
    });
  });
}
