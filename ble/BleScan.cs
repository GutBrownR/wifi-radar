// Escáner Bluetooth LE pasivo: escucha los anuncios que los equipos ya emiten.
// No se conecta, no empareja, no envía datos. Imprime JSON por stdout.
// Uso: BleScan.exe [segundos]
using System;
using System.Collections.Concurrent;
using System.Threading;
using Windows.Devices.Bluetooth.Advertisement;

public static class BleScan
{
    static string Clean(string s)
    {
        if (s == null) return "";
        var sb = new System.Text.StringBuilder();
        foreach (char c in s)
            if (c >= 32 && c != '"' && c != '\\') sb.Append(c);
        return sb.ToString();
    }

    public static int Main(string[] args)
    {
        int seconds = 6;
        if (args.Length > 0) int.TryParse(args[0], out seconds);
        var seen = new ConcurrentDictionary<ulong, string>();
        var w = new BluetoothLEAdvertisementWatcher();
        w.ScanningMode = BluetoothLEScanningMode.Active;
        var handler = new Windows.Foundation.TypedEventHandler<BluetoothLEAdvertisementWatcher, BluetoothLEAdvertisementReceivedEventArgs>((s, a) =>
        {
            int company = -1;
            if (a.Advertisement.ManufacturerData.Count > 0)
                company = a.Advertisement.ManufacturerData[0].CompanyId;
            string name = Clean(a.Advertisement.LocalName);
            string old;
            if (name == "" && seen.TryGetValue(a.BluetoothAddress, out old) && !old.Contains("\"name\":\"\""))
                return; // conserva el registro que ya trae nombre
            seen[a.BluetoothAddress] = string.Format(
                "{{\"addr\":\"{0:x12}\",\"name\":\"{1}\",\"rssi\":{2},\"company\":{3},\"addrType\":\"{4}\"}}",
                a.BluetoothAddress, name, a.RawSignalStrengthInDBm, company, a.BluetoothAddressType);
        });
        var t = typeof(BluetoothLEAdvertisementWatcher);
        var add = (Func<Windows.Foundation.TypedEventHandler<BluetoothLEAdvertisementWatcher, BluetoothLEAdvertisementReceivedEventArgs>, System.Runtime.InteropServices.WindowsRuntime.EventRegistrationToken>)
            Delegate.CreateDelegate(typeof(Func<Windows.Foundation.TypedEventHandler<BluetoothLEAdvertisementWatcher, BluetoothLEAdvertisementReceivedEventArgs>, System.Runtime.InteropServices.WindowsRuntime.EventRegistrationToken>), w, t.GetMethod("add_Received"));
        var rem = (Action<System.Runtime.InteropServices.WindowsRuntime.EventRegistrationToken>)
            Delegate.CreateDelegate(typeof(Action<System.Runtime.InteropServices.WindowsRuntime.EventRegistrationToken>), w, t.GetMethod("remove_Received"));
        System.Runtime.InteropServices.WindowsRuntime.WindowsRuntimeMarshal.AddEventHandler(add, rem, handler);
        w.Start();
        Thread.Sleep(seconds * 1000);
        w.Stop();
        Console.Write("[" + string.Join(",", seen.Values) + "]");
        return 0;
    }
}
