import {graphql} from 'react-relay';
import {commitMutation} from 'relay-runtime';

import type {
  SetSettingMutation,
  SetSettingMutation$data,
  SetSettingMutation$variables,
} from '../__generated__/SetSettingMutation.graphql';
import {environment} from './environment';

// The whole refreshed list comes back, not just the written row: applying a
// setting can drop caches that change what other rows mean, and the caller
// wants to render against the state the server actually holds.
const mutation = graphql`
  mutation SetSettingMutation($key: String!, $value: String!, $allowManaged: Boolean!) {
    setSetting(key: $key, value: $value, allowManaged: $allowManaged) {
      note
      settings {
        id
        key
        value
        updatedAt
        isSet
        label
        description
        managedBy
        kind
        choices
        placeholder
        restartRequired
        known
      }
    }
  }
`;

export function commitSetSetting(variables: SetSettingMutation$variables) {
  return new Promise<SetSettingMutation$data['setSetting']>((resolve, reject) => {
    commitMutation<SetSettingMutation>(environment, {
      mutation,
      variables,
      onCompleted: (res, errors) =>
        errors?.length ? reject(errors[0]) : resolve(res.setSetting),
      onError: reject,
    });
  });
}
