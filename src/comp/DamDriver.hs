-- Native BSC orchestration. C++ generation/linking use the existing simLink;
-- the only simulation subprocess is the compiled DAM runtime or Bluesim.
module DamDriver (damLink, damAfterCompile) where
import Flags (Flags(..))
import Backend (Backend(..))
import Error (ErrorHandle)
import DamPlan
import System.Environment
import System.Directory
import System.FilePath
import System.Process
import System.Exit
import System.IO (withFile, IOMode(WriteMode))
import Control.Exception (bracket)
import Control.Monad
import Data.List (sort, intercalate, isPrefixOf, isSuffixOf)
import Text.Read (readMaybe)

type Link = ErrorHandle -> Flags -> String -> [String] -> [String] -> IO ()

check :: Bool -> String -> IO ()
check ok msg = unless ok (ioError (userError ("DAM: " ++ msg)))

withEnv :: [(String,Maybe String)] -> IO a -> IO a
withEnv settings action = bracket save restore (const (set settings >> action))
  where
    save = mapM (\(k,_) -> do v <- lookupEnv k; return (k,v)) settings
    set = mapM_ (\(k,v) -> maybe (unsetEnv k) (setEnv k) v)
    restore = set

cleanSettings :: IO [(String,Maybe String)]
cleanSettings = do
  env <- getEnvironment
  return [(k,Nothing) | (k,_) <- env, "DAM_" `isPrefixOf` k || "BSC_DAM_" `isPrefixOf` k]

readPlan :: FilePath -> IO DamPlan
readPlan path = do
  text <- readFile path
  case readMaybe text of
    Just p -> return p
    Nothing -> ioError (userError ("DAM: invalid compiler plan " ++ path))

writeExecutable :: FilePath -> String -> IO ()
writeExecutable path text = do
  writeFile path text
  perms <- getPermissions path
  setPermissions path perms { executable = True }

quote :: String -> String
quote s = "'" ++ concatMap (\c -> if c == '\'' then "'\\''" else [c]) s ++ "'"

runLogged :: FilePath -> [String] -> FilePath -> IO ()
runLogged exe args logPath = do
  -- No shell interpolation: filenames and arguments remain separate.
  status <- withFile logPath WriteMode $ \h -> do
    (_,_,_,process) <- createProcess (proc exe args) {
        std_in=NoStream, std_out=UseHandle h, std_err=UseHandle h }
    waitForProcess process
  check (status == ExitSuccess) ("execution failed; see " ++ logPath)

-- With -dam, source compilation can immediately link a single -g top. BDPI
-- inputs use the normal two-step BSC compile/link interface.
damAfterCompile :: Link -> ErrorHandle -> Flags -> IO ()
damAfterCompile link errh flags = when (damEnable flags) $ do
  check (backend flags == Just Bluesim) "-dam requires -sim"
  case genName flags of
    [top] -> damLink link errh flags {genName=[], entry=Just top} top [] []
    _ -> check False "source compilation with -dam requires exactly one -g top"

damLink :: Link -> ErrorHandle -> Flags -> String -> [String] -> [String] -> IO ()
damLink link errh flags top abins native
  | not (damEnable flags) = link errh flags top abins native
  | otherwise = do
      check (not (genSysC flags)) "SystemC execution is unsupported"
      check (damCycles flags > 0 && damCycles flags < 4294967296) "cycle limit must fit a positive 32-bit count"
      exe <- getExecutablePath
      runtime <- makeAbsolute (if null (damRuntime flags)
                    then takeDirectory (takeDirectory exe) </> "lib" </> "DAM"
                    else damRuntime flags)
      mapM_ (\p -> doesFileExist (runtime </> p) >>= \ok -> check ok ("missing installed runtime artifact: " ++ runtime </> p))
        ["dam-runtime", "dam_runtime.o", "include/dam_fifo.h", "include/dam_reg.h", "include/dam_regfile.h"]
      output <- makeAbsolute (oFile flags)
      let folder = output ++ ".dam"
      createDirectoryIfMissing True folder
      settings <- cleanSettings
      withEnv settings $ build runtime output folder
  where
    build runtime output folder = do
      let base = flags {damEnable=False, cdir=Nothing, infoDir=Nothing}
          model :: String -> Maybe Int -> Bool -> [String] -> IO DamPlan
          model name selection owner declarations = do
            let obj = folder </> name
            createDirectoryIfMissing True obj
            -- Prevent accidental reuse of a schedule or header from a previous
            -- layout. Only this driver's generated directory is refreshed.
            contents <- listDirectory obj
            forM_ contents $ \file -> when (any (`isSuffixOf` file) [".o",".cxx",".h"]) (removeFile (obj </> file))
            let includes = ["-include",runtime </> "include/dam_fifo.h",
                            "-include",runtime </> "include/dam_reg.h",
                            "-include",runtime </> "include/dam_regfile.h",
                            "-I" ++ runtime </> "include", "-I" ++ bluespecDir flags </> "Bluesim"]
                workerFlags = base {cdir=Just obj, infoDir=Just obj,
                    oFile=folder </> (name ++ "-model"),
                    cxxFlags=cxxFlags base ++ ["-std=c++17","-O2"] ++ includes,
                    cLibs=if owner then cLibs base else [],
                    cLibPath=if owner then cLibPath base else [],
                    linkFlags=if owner then linkFlags base else []}
                env = [("BSC_DAM_MANIFEST",Just (obj </> "manifest.json")),
                       ("BSC_DAM_PLAN",Just (obj </> "plan")),
                       ("BSC_DAM_PARTITION",show <$> selection)]
            sources <- if owner then return native else do
              let stub = obj </> "bdpi_stubs.cxx"
                  bodies = [takeWhile (/=';') d ++ " { std::abort(); }" | d <- declarations]
              writeFile stub ("#include <cstdlib>\nextern \"C\" {\n" ++ unlines bodies ++ "}\n")
              return [stub]
            withEnv env (link errh workerFlags top abins (sources ++ [runtime </> "dam_runtime.o"]))
            readPlan (obj </> "plan")
      p <- model "reference" Nothing True []
      let count = length (rules p)
          nativeInputs = not (null native && null (cLibs flags) && null (linkFlags flags))
      check (not nativeInputs || not (null (foreignDecls p)))
            "native inputs without tracked BDPI calls cannot be assigned to a dam_context"
      check (count == length (requiredContexts p) + maybe 0 (const 1) (mainWorker p))
            "each worker must be an annotated context or the single main worker"
      check (damWorkers flags == 0 || count <= damWorkers flags)
            ("dam_context requirements need " ++ show count ++
             " workers, exceeding -dam-workers " ++ show (damWorkers flags))
      check (null (foreignDecls p) || length (filter id (foreignOwners p)) == 1)
            "foreign code must have exactly one owner"
      copyFile (folder </> "reference/manifest.json") (folder </> "manifest.json")
      forM_ [0..count-1] $ \n -> do
        let owner = null (foreignDecls p) || foreignOwners p !! n
        got <- model ("partition-" ++ show n) (Just n) owner (foreignDecls p)
        check (got == p) "worker analysis changed during compilation"
        writeExecutable (folder </> ("worker-" ++ show n))
          ("#!/bin/sh\nexec \"$(dirname -- \"$0\")/partition-" ++ show n ++ "-model\" \"$@\"\n")
      writeExecutable output (launcher runtime folder (damCycles flags))
      writeFile (folder </> "decision.json")
        ("{\"execution\":\"dam\",\"policy\":\"explicit_contexts\",\"workers\":" ++ show count ++
         ",\"main_partition\":" ++ maybe "null" show (mainWorker p) ++ "}\n")
      putStrLn ("DAM executable created: " ++ output)
      when (damVerify flags) (verify runtime folder p (damCycles flags))
      when (damRun flags && not (damVerify flags)) $ do
        runLogged output [] (folder </> "run.log")
        readFile (folder </> "run.log") >>= putStr

launcher :: FilePath -> FilePath -> Integer -> String
launcher runtime folder cycles = unlines
  ["#!/bin/sh", "set -eu", "cycles=" ++ show cycles,
   "if [ \"$#\" -ne 0 ]; then",
   "  if [ \"$#\" -ne 2 ] || [ \"$1\" != -m ]; then echo 'Usage: simulator [-m cycles]' >&2; exit 2; fi",
   "  cycles=$2", "fi",
   "export DAM_SKIP_CLOCKS=\"${DAM_SKIP_CLOCKS:-1}\"",
   "out=" ++ quote (folder </> "run"), "mkdir -p \"$out\"",
   "rm -f \"$out\"/worker-*.log \"$out\"/worker-*.trace \"$out\"/worker-*.state",
   quote (runtime </> "dam-runtime") ++ " " ++ quote (folder </> "manifest.json") ++ " " ++ quote folder ++ " \"$out\" \"$cycles\" >\"$out/runtime.log\" 2>&1",
   "for f in \"$out\"/worker-*.log; do cat \"$f\"; done"]

verify :: FilePath -> FilePath -> DamPlan -> Integer -> IO ()
verify runtime folder p cycles = do
  let out = folder </> "validation"
      names = intercalate "," [n ++ ":" ++ show w | (n,w,_,_,_) <- channels p]
      base = [("DAM_CHANNELS",Just names),("DAM_CYCLES",Just (show cycles)),
              ("DAM_CHECK_STATE",Just "1"),("DAM_TRACE",Just (out </> "reference.trace")),
              ("DAM_STATE_TRACE",Just (out </> "reference.state"))]
  createDirectoryIfMissing True out
  withEnv base $ do
    runLogged (folder </> "reference-model") [] (out </> "reference.log")
    expected <- lines <$> readFile (out </> "reference.trace")
    state <- lines <$> readFile (out </> "reference.state")
    output <- readFile (out </> "reference.log")
    forM_ [("dam","0",Nothing),("skip","1",Nothing),("skip-jitter","1",Just "1")] $ \(mode,skip,jitter) -> do
      let dir = out </> mode
      createDirectoryIfMissing True dir
      files <- listDirectory dir
      forM_ files $ \f -> when ("worker-" `isPrefixOf` f) (removeFile (dir </> f))
      withEnv [("DAM_SKIP_CLOCKS",Just skip),("DAM_HOST_JITTER",jitter)] $
        runLogged (runtime </> "dam-runtime") [folder </> "manifest.json",folder,dir,show cycles] (out </> (mode ++ ".log"))
      let collect suffix = concat <$> mapM (readFile . (dir </>) . (\n -> "worker-" ++ show n ++ suffix)) [0..length (rules p)-1]
      transfers <- lines <$> collect ".trace"
      writes <- lines <$> collect ".state"
      text <- collect ".log"
      check (sort transfers == sort expected) (mode ++ " FIFO cycle/value mismatch")
      check (sort writes == sort state) (mode ++ " register/physical-time mismatch")
      check (text == output) (mode ++ " ordered output mismatch")
  writeFile (out </> "validation.json")
    ("{\"passed\":true,\"cycles\":" ++ show cycles ++
     ",\"checks\":[\"FIFO cycle/channel/value\",\"register cycle/physical-time/name/value\",\"complete ordered output\"],\"modes\":[\"dam\",\"skip\",\"skip-jitter\"]}\n")
  putStrLn "DAM cycle-equivalence check passed (off/on/host jitter)"
