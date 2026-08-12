-- Answers one script-message, so the shim can tell whether this mpv can run
-- lua AT ALL. See PlayerManager.lua_works.
--
-- A probe rather than a build-string check: `mpv-configuration` does not
-- mention lua on every build (measured), and `load-script` on a script that
-- cannot run fails SILENTLY -- no exception on either backend -- so the only
-- honest answer is whether a script actually reported back. That also
-- catches lua that is present but broken, which a capability string never
-- would.
mp.commandv('script-message', 'jms-lua', 'ok')
