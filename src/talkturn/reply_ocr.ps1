param([Parameter(Mandatory=$true)][string]$ImagePath)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$bitmap = $null
$stream = $null
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
    $awaitMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
            $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } |
        Select-Object -First 1
    function Await-Local($operation, $resultType) {
        $task = $awaitMethod.MakeGenericMethod($resultType).Invoke($null, @($operation))
        $task.Wait()
        $task.Result
    }
    # StorageFile requires a Windows absolute path, not slash-separated URI syntax.
    $path = [System.IO.Path]::GetFullPath($ImagePath)
    $file = Await-Local ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Await-Local ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await-Local ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await-Local ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if ($null -eq $engine) { throw 'Local OCR is unavailable' }
    $result = Await-Local ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $words = [System.Collections.Generic.List[object]]::new()
    $lineIndex = 0
    foreach ($line in $result.Lines) {
        foreach ($word in $line.Words) {
            $box = $word.BoundingRect
            $words.Add(@{line=$lineIndex; text=$word.Text; x=$box.X; y=$box.Y; width=$box.Width; height=$box.Height})
        }
        $lineIndex++
    }
    @{words=@($words.ToArray()); angle=$result.TextAngle} | ConvertTo-Json -Depth 4 -Compress
} catch {
    [Console]::Error.WriteLine('Local OCR failed')
    exit 1
} finally {
    if ($null -ne $bitmap) { $bitmap.Dispose() }
    if ($null -ne $stream) { $stream.Dispose() }
}
