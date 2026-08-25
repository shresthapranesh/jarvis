import {graphql} from 'react-relay';

export const maintenanceQuery = graphql`
  query MaintenanceQuery {
    checkpointStats {
      dbPath
      exists
      sizeBytes
      threads
      checkpoints
      subgraphCheckpoints
      prunableRoot
      prunableSubgraph
      reclaimableBytes
      threadsSkippedActive
      activeThreads
    }
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
