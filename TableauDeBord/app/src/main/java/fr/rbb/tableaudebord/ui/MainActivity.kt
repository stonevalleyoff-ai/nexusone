package fr.rbb.tableaudebord.ui

import android.os.Bundle
import android.os.SystemClock
import android.view.WindowManager
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.IntentSenderRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.outlined.CalendarMonth
import androidx.compose.material.icons.outlined.Checklist
import androidx.compose.material.icons.outlined.Folder
import androidx.compose.material.icons.outlined.Home
import androidx.compose.material.icons.outlined.Lightbulb
import androidx.compose.material.icons.outlined.OpenInNew
import androidx.compose.material.icons.outlined.Share
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.fragment.app.FragmentActivity
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import fr.rbb.tableaudebord.Config
import fr.rbb.tableaudebord.net.DriveFichier
import fr.rbb.tableaudebord.ui.screens.EcranAccueil
import fr.rbb.tableaudebord.ui.screens.EcranAgenda
import fr.rbb.tableaudebord.ui.screens.EcranConseils
import fr.rbb.tableaudebord.ui.screens.EcranDocuments
import fr.rbb.tableaudebord.ui.screens.EcranPdf
import fr.rbb.tableaudebord.ui.screens.EcranTaches
import fr.rbb.tableaudebord.ui.theme.TdbTheme

private enum class Onglet(val route: String, val titre: String, val icone: ImageVector) {
    Accueil("accueil", "Accueil", Icons.Outlined.Home),
    Taches("taches", "Tâches", Icons.Outlined.Checklist),
    Agenda("agenda", "Agenda", Icons.Outlined.CalendarMonth),
    Documents("documents", "Documents", Icons.Outlined.Folder),
    Conseils("conseils", "Conseils", Icons.Outlined.Lightbulb),
}

class MainActivity : FragmentActivity() {

    private val vm: AppViewModel by viewModels()
    private var deverrouille by mutableStateOf(false)
    private var quitteA = 0L
    private var premiereSynchroFaite = false

    private val consentement = registerForActivityResult(ActivityResultContracts.StartIntentSenderForResult()) { r ->
        vm.apresConsentement(this, r.data)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Rotation de l'écran : on ne redemande pas l'empreinte.
        deverrouille = savedInstanceState?.getBoolean("deverrouille") ?: false
        premiereSynchroFaite = savedInstanceState?.getBoolean("synchro") ?: false
        // Pas de capture d'écran ni d'aperçu dans les applis récentes.
        window.setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE)
        enableEdgeToEdge()
        setContent {
            TdbTheme {
                if (!deverrouille) {
                    EcranVerrou(this) { surDeverrouillage() }
                } else {
                    val pi by vm.consentement.collectAsStateWithLifecycle()
                    LaunchedEffect(pi) {
                        pi?.let {
                            vm.consentementLance()
                            consentement.launch(IntentSenderRequest.Builder(it.intentSender).build())
                        }
                    }
                    Racine(vm, actualiser = { vm.synchroniser(this) })
                }
            }
        }
    }

    private fun surDeverrouillage() {
        deverrouille = true
        if (!premiereSynchroFaite) {
            premiereSynchroFaite = true
            vm.synchroniser(this)
        }
    }

    override fun onStart() {
        super.onStart()
        if (deverrouille && quitteA > 0 && SystemClock.elapsedRealtime() - quitteA > Config.VERROU_APRES_MS) {
            deverrouille = false
            premiereSynchroFaite = false // nouvelle ouverture = nouvelle synchro
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putBoolean("deverrouille", deverrouille)
        outState.putBoolean("synchro", premiereSynchroFaite)
    }

    override fun onStop() {
        super.onStop()
        quitteA = SystemClock.elapsedRealtime()
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun Racine(vm: AppViewModel, actualiser: () -> Unit) {
    val contexte = LocalContext.current
    val nav = rememberNavController()
    val vue by vm.vue.collectAsStateWithLifecycle()
    val synchro by vm.synchro.collectAsStateWithLifecycle()
    val agenda by vm.agenda.collectAsStateWithLifecycle()
    val docs by vm.docs.collectAsStateWithLifecycle()
    val questions by vm.questions.collectAsStateWithLifecycle()
    val pdf by vm.pdf.collectAsStateWithLifecycle()
    val snack = remember { SnackbarHostState() }
    var retoursEnAttente by remember { mutableStateOf(false) }

    LaunchedEffect(synchro.message) {
        synchro.message?.let { snack.showSnackbar(it) }
    }
    LaunchedEffect(synchro.enCours, questions) {
        retoursEnAttente = (contexte.applicationContext as fr.rbb.tableaudebord.TdbApp).depot.retoursEnAttente
    }

    val ouvert = pdf
    if (ouvert != null) {
        BackHandler { vm.fermerPdf() }
        Scaffold(
            topBar = {
                TopAppBar(
                    title = { Text(ouvert.second, maxLines = 1) },
                    navigationIcon = {
                        IconButton(onClick = { vm.fermerPdf() }) {
                            Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Fermer")
                        }
                    },
                    actions = {
                        IconButton(onClick = { partager(contexte, ouvert.first, ouvert.second) }) {
                            Icon(Icons.Outlined.Share, contentDescription = "Partager")
                        }
                        IconButton(onClick = { ouvrirAvecAutreAppli(contexte, ouvert.first) }) {
                            Icon(Icons.Outlined.OpenInNew, contentDescription = "Ouvrir avec une autre appli")
                        }
                    },
                )
            },
        ) { marges ->
            Box(Modifier.padding(marges)) { EcranPdf(ouvert.first) }
        }
        return
    }

    val entree by nav.currentBackStackEntryAsState()
    val routeCourante = entree?.destination?.route

    Scaffold(
        topBar = {
            Column {
                TopAppBar(
                    title = { Text(Onglet.entries.firstOrNull { it.route == routeCourante }?.titre ?: "Tableau de bord") },
                    actions = {
                        IconButton(onClick = actualiser, enabled = !synchro.enCours) {
                            Icon(Icons.Filled.Refresh, contentDescription = "Actualiser")
                        }
                    },
                )
                if (synchro.enCours) LinearProgressIndicator(Modifier.fillMaxWidth())
            }
        },
        bottomBar = {
            NavigationBar {
                Onglet.entries.forEach { o ->
                    NavigationBarItem(
                        selected = routeCourante == o.route,
                        onClick = {
                            nav.navigate(o.route) {
                                popUpTo(nav.graph.findStartDestination().id) { saveState = true }
                                launchSingleTop = true
                                restoreState = true
                            }
                        },
                        icon = {
                            if (o == Onglet.Taches && vue.aTraiter.isNotEmpty()) {
                                androidx.compose.material3.BadgedBox(badge = {
                                    androidx.compose.material3.Badge { Text(vue.aTraiter.size.toString()) }
                                }) { Icon(o.icone, contentDescription = o.titre) }
                            } else {
                                Icon(o.icone, contentDescription = o.titre)
                            }
                        },
                        label = { Text(o.titre) },
                    )
                }
            }
        },
        snackbarHost = { SnackbarHost(snack) },
    ) { marges ->
        NavHost(nav, startDestination = Onglet.Accueil.route, modifier = Modifier.padding(marges).fillMaxSize()) {
            composable(Onglet.Accueil.route) {
                EcranAccueil(
                    vue = vue,
                    versTaches = { nav.navigate(Onglet.Taches.route) { launchSingleTop = true } },
                    versDocuments = { nav.navigate(Onglet.Documents.route) { launchSingleTop = true } },
                    ouvrirDocument = { id, titre ->
                        val f = DriveFichier(id, titre, if (titre.endsWith(".pdf", true)) "application/pdf" else "")
                        vm.telecharger(f) { fichier ->
                            if (f.estPdf) vm.ouvrirPdf(fichier, titre) else ouvrirAvecAutreAppli(contexte, fichier)
                        }
                    },
                )
            }
            composable(Onglet.Taches.route) {
                EcranTaches(vue.taches, marquer = vm::marquer, annuler = vm::annuler)
            }
            composable(Onglet.Agenda.route) {
                EcranAgenda(agenda, vue.echeances.map { it.first })
            }
            composable(Onglet.Documents.route) {
                BackHandler(enabled = docs.pile.size > 1 || docs.recherche.isNotEmpty()) { vm.remonter() }
                EcranDocuments(
                    etat = docs,
                    ouvrirDossier = vm::ouvrirDossier,
                    remonter = { vm.remonter() },
                    chercher = vm::chercher,
                    telecharger = vm::telecharger,
                    ouvrirPdf = vm::ouvrirPdf,
                )
            }
            composable(Onglet.Conseils.route) {
                EcranConseils(vue.conseils, questions, retoursEnAttente, poser = vm::poserQuestion)
            }
        }
    }
}
