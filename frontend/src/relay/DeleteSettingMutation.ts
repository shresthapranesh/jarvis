import {graphql} from 'react-relay';
import {commitMutation} from 'relay-runtime';

import type {
  DeleteSettingMutation,
  DeleteSettingMutation$data,
  DeleteSettingMutation$variables,
} from '../__generated__/DeleteSettingMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation DeleteSettingMutation($key: String!, $allowManaged: Boolean!) {
    deleteSetting(key: $key, allowManaged: $allowManaged) {
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

export function commitDeleteSetting(variables: DeleteSettingMutation$variables) {
  return new Promise<DeleteSettingMutation$data['deleteSetting']>((resolve, reject) => {
    commitMutation<DeleteSettingMutation>(environment, {
      mutation,
      variables,
      onCompleted: (res, errors) =>
        errors?.length ? reject(errors[0]) : resolve(res.deleteSetting),
      onError: reject,
    });
  });
}
