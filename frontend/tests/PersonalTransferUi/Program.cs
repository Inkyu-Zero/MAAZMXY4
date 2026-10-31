using Avalonia;
using Avalonia.Controls;
using Avalonia.Headless;
using Avalonia.Interactivity;
using Avalonia.LogicalTree;
using Avalonia.Media.Imaging;
using Avalonia.Themes.Fluent;
using Avalonia.Threading;
using MFAAvalonia.Services;
using MFAAvalonia.Views.UserControls;

internal static class Program
{
    [STAThread] static int Main(string[] args)
    {
        var root=Path.Combine(Path.GetTempPath(),"personal-transfer-ui-"+Guid.NewGuid().ToString("N"));
        try
        {
            StartupContract.Check();
            AppBuilder.Configure<TestApp>().UseSkia().UseHeadless(new AvaloniaHeadlessPlatformOptions{UseHeadlessDrawing=false}).SetupWithoutStarting();
            Directory.CreateDirectory(Path.Combine(root,"config"));Directory.CreateDirectory(args[0]);
            File.WriteAllText(Path.Combine(root,"config/config.json"),"{\"test\":true}");
            var transfer=new PersonalTransfer(root);var active=0;
            var view=new PersonalTransferView(transfer,()=>{active++;return new Lease(()=>active--);});
            var window=new Window{Content=new ScrollViewer{Content=view},Width=600,Height=720};window.Show();
            void Draw(string name,int width){window.Width=width;Dispatcher.UIThread.RunJobs();window.UpdateLayout();using var bmp=new RenderTargetBitmap(new PixelSize(width,720),new Vector(96,96));bmp.Render(window);bmp.Save(Path.Combine(args[0],name+".png"));}
            void Pump(Task task){var stop=DateTime.UtcNow.AddSeconds(30);while(!task.IsCompleted){if(DateTime.UtcNow>stop)throw new TimeoutException();Dispatcher.UIThread.RunJobs();Thread.Sleep(10);}task.GetAwaiter().GetResult();Dispatcher.UIThread.RunJobs();}
            if(view.GetLogicalDescendants().OfType<TextBox>().Any())throw new Exception("External tool picker must be removed");
            if(!view.GetLogicalDescendants().OfType<Button>().Any(b=>Equals(b.Content,"导出 ZIP…")))throw new Exception("ZIP export button missing");
            var archive=Path.Combine(root,"export.zip");Pump(view.ExportToAsync(archive));
            File.Copy(archive,Path.Combine(args[0],"sample.zip"),true);
            Pump(view.LoadPreviewAsync(archive));Draw("import-preview-wide",600);Draw("import-preview-narrow",360);
            var confirm=view.GetLogicalDescendants().OfType<Button>().Single(b=>Equals(b.Content,"确认导入，下次启动生效"));
            if(!confirm.IsVisible)throw new Exception("Preview did not expose confirmation");
            confirm.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            var until=DateTime.UtcNow.AddSeconds(10);while(active!=0){if(DateTime.UtcNow>until)throw new TimeoutException();Dispatcher.UIThread.RunJobs();Thread.Sleep(10);}
            if(!transfer.HasPending || confirm.IsVisible)throw new Exception("Confirmation did not stage import");
            Draw("import-staged",360);
            var cancel=view.GetLogicalDescendants().OfType<Button>().Single(b=>Equals(b.Content,"取消待导入备份"));cancel.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));Dispatcher.UIThread.RunJobs();
            if(transfer.HasPending || cancel.IsVisible)throw new Exception("Cancel did not clear pending import");
            window.Close();Console.WriteLine("PASS real compiled transfer controls: ZIP export, preview, confirmation, cancel, wide/narrow rendering; headless only.");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
        finally{if(Directory.Exists(root))Directory.Delete(root,true);}
    }
}
internal sealed class TestApp:Application { public override void Initialize()=>Styles.Add(new FluentTheme()); }
internal sealed class Lease(Action release):IDisposable { public void Dispose()=>release(); }
