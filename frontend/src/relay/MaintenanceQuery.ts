import {graphql} from 'react-relay';

export const maintenanceQuery = graphql`
  query MaintenanceQuery {
    voiceStatus {
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
