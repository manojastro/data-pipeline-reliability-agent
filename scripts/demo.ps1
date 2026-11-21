$ErrorActionPreference = 'Stop'
$BaseUri = 'http://127.0.0.1:8103'
if (-not $env:API_KEY) { $env:API_KEY = 'local-demo-key' }
$Headers = @{ 'X-API-Key' = $env:API_KEY }
$Run = Invoke-RestMethod -Method Post -Uri "$BaseUri/api/v1/demo/run" -Headers $Headers -ContentType 'application/json' -Body '{"scenario":"incorrect_join"}'
$ProposalId = $Run.rca.recommended_action.proposal_id
$ApprovalBody = @{ decision = 'APPROVED'; reason = 'Exact artifact and isolated shadow evidence reviewed' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$BaseUri/api/v1/proposals/$ProposalId/approval" -Headers $Headers -ContentType 'application/json' -Body $ApprovalBody | Out-Null
$Execution = Invoke-RestMethod -Method Post -Uri "$BaseUri/api/v1/proposals/$ProposalId/execute" -Headers $Headers
[pscustomobject]@{
    PipelineStatus = $Run.pipeline_status
    IncidentKind = $Run.incident_kind
    RootCause = $Run.rca.probable_root_cause
    BeforeScore = $Run.validation.before_score
    AfterScore = $Execution.verified_quality_score
    Execution = $Execution.status
}
