param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs = @()
)

$projectName = "pulse-tests-$([guid]::NewGuid().ToString('N'))"
$composeArgs = @("-p", $projectName, "-f", "docker-compose.test.yml")
$testExitCode = 1

try {
    docker compose @composeArgs run --build --rm tests @PytestArgs
    $testExitCode = $LASTEXITCODE
}
finally {
    docker compose @composeArgs down --volumes --remove-orphans
}

exit $testExitCode