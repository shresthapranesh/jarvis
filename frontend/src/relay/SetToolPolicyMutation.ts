import {graphql} from 'react-relay';
import {commitMutation} from 'relay-runtime';

import type {
  SetToolPolicyMutation,
  SetToolPolicyMutation$variables,
} from '../__generated__/SetToolPolicyMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation SetToolPolicyMutation($key: String!, $enabled: Boolean, $requiresApproval: Boolean) {
    setToolPolicy(key: $key, enabled: $enabled, requiresApproval: $requiresApproval) {
      id
      key
      kind
      name
      description
      group
      enabled
      requiresApproval
      inPrompt
      available
      detail
    }
  }
`;

export function commitSetToolPolicy(variables: SetToolPolicyMutation$variables) {
  return new Promise<void>((resolve, reject) => {
    commitMutation<SetToolPolicyMutation>(environment, {
      mutation,
      variables,
      onCompleted: (_res, errors) => (errors?.length ? reject(errors[0]) : resolve()),
      onError: reject,
    });
  });
}
