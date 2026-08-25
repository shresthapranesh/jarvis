import {graphql} from 'react-relay';
import {commitMutation} from 'relay-runtime';

import type {
  DownloadVoiceMutation,
  DownloadVoiceMutation$data,
  DownloadVoiceMutation$variables,
} from '../__generated__/DownloadVoiceMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation DownloadVoiceMutation($force: Boolean!) {
    downloadVoice(force: $force) {
      voice
      directory
      ready
      error
      files {
        name
        path
        exists
        sizeBytes
        downloaded
      }
    }
  }
`;

export function commitDownloadVoice(variables: DownloadVoiceMutation$variables) {
  return new Promise<DownloadVoiceMutation$data['downloadVoice']>((resolve, reject) => {
    commitMutation<DownloadVoiceMutation>(environment, {
      mutation,
      variables,
      onCompleted: (res, errors) =>
        errors?.length ? reject(errors[0]) : resolve(res.downloadVoice),
      onError: reject,
    });
  });
}
